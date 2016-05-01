#!/usr/bin/env python

import os
import sys

import numpy as np
import pandas as pd
import xgboost as xgb

from sklearn import cross_validation, metrics   #Additional scklearn functions
from sklearn.ensemble import RandomForestClassifier, RandomForestRegressor, ExtraTreesClassifier, ExtraTreesRegressor
from sklearn.calibration import CalibratedClassifierCV
from sklearn.grid_search import GridSearchCV   #Perforing grid search
from sklearn.metrics import roc_auc_score, log_loss, make_scorer
from sklearn.feature_selection import SelectFromModel

from utils import log, INFO, WARN
from load import load_data, data_transform_2, load_cache, save_cache, load_interaction_information, load_feature_importance, save_kaggle_submission

BASEPATH = os.path.dirname(os.path.abspath(__file__))

class ParameterTuning(object):
    def __init__(self, target, data_id, method, n_estimator, cost, objective, cv, n_jobs):
        self.target = target
        self.data_id = data_id
        self.method = method

        self.n_estimator = n_estimator
        self.cost = cost
        if self.cost == "log_loss":
            self.cost_function = log_loss
        elif self.cost == "auc":
            self.cost_function = roc_auc_score

        self.objective = objective
        self.cv = cv
        self.n_jobs = n_jobs

        self.random_state = 1201

        self.best_cost = -np.inf

        self.train = None
        self.train_selector = None

        self.done = {}

    def set_filepath(self, filepath, filepath_testing):
        self.filepath = filepath
        self.filepath_testing = filepath_testing

    def set_dataset(self, train, train_y, test_id, test_x):
        self.train = train
        self.train_y = train_y
        self.test_id = test_id
        self.test_x = test_x

        predictors = [x for x in self.train.columns if x not in [self.target, self.data_id]]
        self.predictors = predictors

    def save(self):
        save_cache(self.done, self.filepath)

    def load(self):
        self.done = load_cache(self.filepath)

        log("The done list contains {}".format(self.done), INFO)

    def compare(self, cost):
        if cost > self.best_cost:
            self.best_cost = cost

            return True
        elif cost == self.best_cost:
            log("The cost is the same with the previous cost - {}".format(cost))

            return True
        else:
            return False

    def get_best_params(self, grid_model, x, y):
        grid_model.fit(x, y)

        return grid_model.best_score_, grid_model.best_params_, grid_model.grid_scores_

    def improve(self, model, phase, cost, params, micro_tuning=False):
        old_cost = self.best_cost

        if self.compare(cost):
            log("Improve the {} from {} to {}".format(self.cost, old_cost, self.best_cost))
            for key, value in params.items():
                setattr(self, key, value)
                log("Set {} to be {}".format(key, getattr(self, key)))

            filepath_testing = self.filepath_testing.replace("submission", phase)
            self.submit(model, filepath_testing, "testing")

            filepath_training = self.filepath_testing.replace("submission", "training_{}".format(phase))
            self.submit(model, filepath_training, "training")
        else:
            if not micro_tuning:
                log("Fail because of the {} of phase2-model is {}(< {}) so setting the params as default".format(self.cost, cost, old_cost), WARN)

                for key, value in params.items():
                    setattr(self, key, getattr(self, "default_{}".format(key)))
            else:
                log("Fail in {}".format(phase), WARN)

        self.save()

    def get_value(self, name):
        return getattr(self, name) if getattr(self, name) else getattr(self, "default_{}".format(name))

    def get_model_instance(self):
        raise NotImeplementError

    def enable_feature_importance(self, filepath_pkl, top_feature=512):
        self.predictors = load_feature_importance(filepath_pkl, top_feature)
        self.predictors = list(set(self.predictors))

    def phase(self, phase, params, is_micro_tuning=False):
        gsearch1 = None
        best_cost, best_params, scores = -np.inf, -np.inf, None
        if phase in self.done:
            log("The {} is done so we skip it".format(phase))
            for key in params.keys():
                log("The {} is {} based on {}".format(key, getattr(self, key), phase))

            infos = self.done[phase]
            if infos:
                best_cost, best_params, scores, gsearch1 = infos
                self.improve(gsearch1, phase, best_cost, best_params)
        else:
            model = self.get_model_instance()
            log("The params are {}".format(model.get_params()), INFO)

            gsearch1 = GridSearchCV(estimator=model,
                                    param_grid=params,
                                    scoring="roc_auc" if self.cost_function.__name__ == "roc_auc_score" else make_scorer(self.cost_function),
                                    n_jobs=self.n_jobs,
                                    iid=False,
                                    cv=self.cv,
                                    verbose=1)

            log("Training by {} features".format(len(self.predictors)), INFO)

            best_cost, best_params, scores = self.get_best_params(gsearch1, self.train[self.predictors], self.train[self.target])
            log("The {} of {}-model is {:.8f} based on {}".format(self.cost_function.__name__, phase, best_cost, best_params.keys()))

            self.done[phase] = best_cost, best_params, scores, gsearch1
            self.improve(gsearch1, phase, best_cost, best_params)

        gsearch2 = None
        micro_cost, micro_params, micro_scores = -np.inf, -np.inf, None
        if is_micro_tuning:
            key = "micro-{}".format(phase)
            if key in self.done:
                log("The {} is done so we skip it".format(key))
                for name in params.keys():
                    log("The {} is {} based on {}".format(name, getattr(self, name), key))

                infos = self.done[key]
                if infos:
                    micro_cost, micro_params, micro_scores, gsearch2 = infos
                    self.improve(gsearch2, key, micro_cost, micro_params, True)
            else:
                advanced_params = {}
                for name, value in best_params.items():
                    if isinstance(value, int):
                        advanced_params[name] = [i for i in range(max(0, value-1), value+1) if i != value]
                    elif value != 0 and isinstance(value, float):
                        if type(self).__name__.lower().find("xgb") != -1 and name in ["gamma", "subsample", "colsample_bytree"]:
                            advanced_params[name] = [min(value*i, 1.0) for i in [0.25, 0.75, 1.25]]
                        else:
                            advanced_params[name] = [value*i for i in [0.25, 0.75, 1.25]]

                if advanced_params:
                    gsearch2 = GridSearchCV(estimator=self.get_model_instance(),
                                            param_grid=advanced_params,
                                            scoring="roc_auc" if self.cost_function.__name__ == "roc_auc_score" else make_scorer(self.cost_function),
                                            n_jobs=self.n_jobs,
                                            iid=False,
                                            cv=self.cv,
                                            verbose=1)

                    micro_cost, micro_params, micro_scores = self.get_best_params(gsearch2, self.train[self.predictors], self.train[self.target])
                    log("Finish the micro-tuning of {}, and then get best params is {}".format(phase, micro_params))

                    self.done[key] = micro_cost, micro_params, micro_scores, gsearch2
                    self.improve(gsearch2, key, micro_cost, micro_params, True)
                else:
                    log("Due to the empty advanced_params so skipping the micro-tunnung", WARN)

        model = None
        a, b, c = None, None, None
        if micro_cost > best_cost:
            model = gsearch2
            a, b, c = micro_cost, micro_params, micro_scores
        else:
            model = gsearch1
            a, b, c = best_cost, best_params, scores

        return a, b, c, model

    def get_training_score(self, model):
        if self.method == "classifier":
            predicted_proba = model.predict_proba(self.train[self.predictors])[:,1]
            cost = self.cost_function(self.train[self.target], predicted_proba)
            log("The {} of training dataset is {:.8f}".format(self.cost, cost))
        elif self.method == "regressor":
            predicted_proba = model.predict(self.train[self.predictors])
            cost = self.cost_function(self.train[self.target], predicted_proba)
            log("The {} of training dataset is {:.8f}".format(self.cost, cost))
        else:
            log("???? {}".format(self.method))

    def process(self):
        raise NotImplementError

    def submit(self, model, filepath_testing, mode="training"):
        results, predicted_proba = None, None

        if mode == "training":
            if self.method == "classifier":
                predicted_proba = model.predict_proba(self.train[self.predictors])[:,1]
            elif self.method == "regressor":
                predicted_proba = model.predict(self.train[self.predictors])
            else:
                raise NotImplementError

            results = {"Target": self.train_y, "Predicted_Proba": predicted_proba}
        else:
            if self.method == "classifier":
                predicted_proba = model.predict_proba(self.test_x[self.predictors])[:,1]
            elif self.method == "regressor":
                predicted_proba = model.predict(self.test_x[self.predictors])
            else:
                raise NotImplementError

            results = {"ID": self.test_id, "TARGET": predicted_proba}

        if not os.path.exists(filepath_testing):
            log("Compile a submission results for kaggle in {}".format(filepath_testing), INFO)
            save_kaggle_submission(results, filepath_testing)

    def calibrated_prediction(self):
        if self.method == "classifier":
            for method_calibration in ["sigmoid", "isotonic"]:
                filepath_testing = self.filepath_testing.replace("submission", "calibrated={}".format(method_calibration))
                filepath_calibration = filepath_testing.replace("csv", "pkl")
                if not os.path.exists(filepath_testing):
                    clf = CalibratedClassifierCV(base_estimator=self.get_model_instance(), cv=self.cv, method=method_calibration)
                    if os.path.exists(filepath_calibration):
                        clf = load_cache(filepath_calibration)
                    else:
                        clf.fit(self.train[self.predictors], self.train_y)
                        save_cache(filepath_calibration)

                    log("Save calibrated results in {}".format(filepath_testing), INFO)
                    self.submit(clf, filepath_testing, "testing")
        else:
            log("Not support calibrated prediction model for {}".format(self.method), WARN)

class RandomForestTuning(ParameterTuning):
    def __init__(self, target, data_id, method, n_estimator=200, cost="logloss", objective="entropy", cv=10, n_jobs=-1):
        ParameterTuning.__init__(self, target, data_id, method, n_estimator, cost, objective, cv, n_jobs)

        self.default_criterion, self.criterion = "entropy", None
        self.default_max_features, self.max_features = 0.5, None
        self.default_max_depth, self.max_depth = 8, None
        self.default_min_samples_split, self.min_samples_split = 4, None
        self.default_min_samples_leaf, self.min_samples_leaf = 2, None
        self.default_class_weight, self.class_weight = None, None

    def get_model_instance(self):
        n_estimator = self.get_value("n_estimator")

        criterion = self.get_value("criterion")
        max_features = self.get_value("max_features")
        max_depth = self.get_value("max_depth")
        min_samples_split = self.get_value("min_samples_split")
        min_samples_leaf = self.get_value("min_samples_leaf")
        class_weight = self.get_value("class_weight")

        if self.method == "classifier":
            return RandomForestClassifier(n_estimators=n_estimator,
                                          criterion=criterion,
                                          max_features=max_features,
                                          max_depth=max_depth,
                                          min_samples_split=min_samples_split,
                                          min_samples_leaf=min_samples_leaf,
                                          class_weight=class_weight,
                                          n_jobs=-1)
        elif self.method == "regressor":
            return RandomForestRegressor(n_estimators=n_estimator,
                                         max_features=max_features,
                                         max_depth=max_depth,
                                         min_samples_split=min_samples_split,
                                         min_samples_leaf=min_samples_leaf,
                                         n_jobs=-1)

    def process(self):
        model = None

        _, _, _, model = self.phase("phase1", {})

        size_feature = len(self.predictors)
        param2 = {'max_depth': range(6, 11, 2), 'max_features': [ratio for ratio in [0.75, 0.1, 0.25, np.sqrt(size_feature)/size_feature]]}
        _, _, _, model = self.phase("phase2", param2, True)

        param3 = {"min_samples_leaf": range(2, 5, 2), "min_samples_split": range(4, 9, 2)}
        _, _, _, model = self.phase("phase3", param3, True)

        if self.method == "classifier":
            param4 = {"class_weight": ["balanced", {0: 1.5, 1: 1}, {0: 2, 1: 1}, {0: 2.5, 1: 1}]}
            _, _, _, model = self.phase("phase4", param4)

        log("The best params are {}".format(model.get_params()), INFO)
        self.calibrated_prediction()

class ExtraTreeTuning(RandomForestTuning):
    def get_model_instance(self):
        n_estimator = self.get_value("n_estimator")

        criterion = self.get_value("criterion")
        max_features = self.get_value("max_features")
        max_depth = self.get_value("max_depth")
        min_samples_split = self.get_value("min_samples_split")
        min_samples_leaf = self.get_value("min_samples_leaf")
        class_weight = self.get_value("class_weight")

        if self.method == "classifier":
            return ExtraTreesClassifier(n_estimators=n_estimator,
                                        criterion=criterion,
                                        max_features=max_features,
                                        max_depth=max_depth,
                                        min_samples_split=min_samples_split,
                                        min_samples_leaf=min_samples_leaf,
                                        class_weight=class_weight,
                                        n_jobs=-1)
        elif self.method == "regressor":
            return ExtraTreesRegressor(n_estimators=n_estimator,
                                       max_features=max_features,
                                       max_depth=max_depth,
                                       min_samples_split=min_samples_split,
                                       min_samples_leaf=min_samples_leaf,
                                       n_jobs=-1)

class XGBoostingTuning(ParameterTuning):
    def __init__(self, target, data_id, method, n_estimator=200, cost="log_loss", objective="binary:logistic", cv=10, n_jobs=-1):
        ParameterTuning.__init__(self, target, data_id, method, n_estimator, cost, objective, cv, n_jobs)

        self.default_learning_rate, self.learning_rate = 0.1, None
        self.default_max_depth, self.max_depth = 5, None
        self.default_min_child_weight, self.min_child_weight = 1, None

        self.default_gamma, self.gamma = 0, None
        self.default_subsample, self.subsample = 0.8, None
        self.default_colsample_bytree, self.colsample_bytree = 0.8, None

        self.default_reg_alpha, self.reg_alpha = 0, None

    def get_model_instance(self):
        learning_rate = self.get_value("learning_rate")
        n_estimator = self.get_value("n_estimator")
        max_depth = self.get_value("max_depth")
        min_child_weight = self.get_value("min_child_weight")
        gamma = self.get_value("gamma")
        subsample = self.get_value("subsample")
        colsample_bytree = self.get_value("colsample_bytree")
        reg_alpha = self.get_value("reg_alpha")

        if self.method == "classifier":
            log("Current parameters - learning_rate: {}, n_estimator: {}, max_depth: {}, min_child_weight: {}, gamma: {}, subsample: {}, colsample_bytree: {}, reg_alpha: {}".format(learning_rate, n_estimator, max_depth, min_child_weight, gamma, subsample, colsample_bytree, reg_alpha))

            return xgb.XGBClassifier(learning_rate=learning_rate,
                                     n_estimators=n_estimator,
                                     max_depth=max_depth,
                                     min_child_weight=min_child_weight,
                                     gamma=gamma,
                                     subsample=subsample,
                                     colsample_bytree=colsample_bytree,
                                     reg_alpha=reg_alpha,
                                     objective=self.objective,
                                     nthread=4,
                                     scale_pos_weight=1,
                                     seed=self.random_state)

        elif self.method == "regressor":
            return xgb.XGBRegressor(learning_rate=learning_rate,
                                    n_estimators=n_estimator,
                                    max_depth=max_depth,
                                    min_child_weight=min_child_weight,
                                    gamma=gamma,
                                    subsample=subsample,
                                    colsample_bytree=colsample_bytree,
                                    reg_alpha=reg_alpha,
                                    objective=self.objective,
                                    nthread=4,
                                    scale_pos_weight=1,
                                    seed=self.random_state)

    def process(self):
        self.phase("phase1", {})

        param2 = {'max_depth':range(7, 14, 2), 'min_child_weight':range(1, 4, 2)}
        self.phase("phase2", param2, True)

        param3 = {'gamma':[i/10.0 for i in range(0, 3)]}
        self.phase("phase3", param3, True)

        param4 = {'subsample':[i/10.0 for i in range(6, 11, 2)], 'colsample_bytree':[i/10.0 for i in range(6, 11, 2)]}
        self.phase("phase4", param4, True)

        param5 = {'reg_alpha':[1e-5, 1e-2, 0.1, 1.0]}
        self.phase("phase5", param5, True)

        self.calibrated_prediction()
