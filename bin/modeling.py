#!/usr/bin/env python

#############################################################################
#
# version 2.0: add feature from KMeans cluster 2016/03/31
#
#############################################################################

import os
import sys
import click
import shutil

import numpy as np

sys.path.append("{}/../lib".format(os.path.dirname(os.path.abspath(__file__))))
from utils import log, INFO, WARN
from load import load_data, load_advanced_data, load_cache, save_cache, save_kaggle_submission, load_interaction_information, load_feature_importance
from learning import LearningFactory
from configuration import ModelConfParser
from deep_learning import KaggleCheckpoint
from keras.callbacks import EarlyStopping
from ensemble_learning import layer_model, final_model, store_layer_output

BASEPATH = os.path.dirname(os.path.abspath(__file__))

@click.command()
@click.option("--conf", required=True, help="Configureation File to this model")
@click.option("--thread", default=1, help="Number of thread")
@click.option("--is-feature-importance", is_flag=True, help="Turn on the feature importance")
@click.option("--is-testing", is_flag=True, help="Turn on the testing mode")
def learning(conf, thread, is_feature_importance, is_testing):
    drop_fields = []

    parser = ModelConfParser(conf)

    BASEPATH = parser.get_workspace()
    objective = parser.get_objective()
    binsize, top = parser.get_interaction_information()
    cost = parser.get_cost()
    nfold = parser.get_nfold()
    top_feature = parser.get_top_feature()

    filepath_training = "{}/input/train.csv".format(BASEPATH)
    filepath_testing = "{}/input/test.csv".format(BASEPATH)
    filepath_cache_1 = "{}/input/train.pkl".format(BASEPATH)
    folder_ii = "{}/input/interaction_information/transform2=True_testing=-1_binsize={}".format(BASEPATH, binsize)
    filepath_feature_importance = "{}/etc/feature_profile/transform2=True_binsize={}_top={}.pkl".format(BASEPATH, binsize, top)

    train_x, test_x, train_y, test_id, train_id = load_data(filepath_cache_1, filepath_training, filepath_testing, drop_fields)
    if is_testing:
        train_x = train_x.head(1000)
        train_y = train_y.head(1000)
    basic_columns = train_x.columns

    for layers, value in load_interaction_information(folder_ii, threshold=top):
        for df in [train_x, test_x]:
            t = value
            breaking_layer = None
            for layer in layers:
                if layer in basic_columns:
                    t *= df[layer].values
                else:
                    breaking_layer = layer
                    break

            if breaking_layer == None:
                df[";".join(layers)] = t
            else:
                log("Skip {} due to {} not in columns".format(layers, breaking_layer), WARN)
                break
    ii_columns = train_x.columns

    importance_columns = []
    if is_feature_importance:
        importance_columns = load_feature_importance(filepath_feature_importance, top_feature)

    predictors = {"basic": basic_columns,
                  "interaction-information-3": [column for column in ii_columns if column.count(";") == 1],
                  "interaction-information-4": [column for column in ii_columns if column.count(";") == 2],
                  "feature-importance": importance_columns}

    train_y = train_y.values
    test_id = test_id.values
    train_Y = train_y.astype(float)

    layer1_models, layer2_models, last_model = [], [], []

    # Init the parameters of deep learning
    checkpointer = KaggleCheckpoint(filepath="{epoch}.weights.hdf5",
                                    training_set=([train_x], train_Y),
                                    testing_set=([test_x], test_id),
                                    folder=None,
                                    cost_string=cost,
                                    verbose=0, save_best_only=True, save_training_dataset=False)

    # Init the parameters of cluster
    for idx, layer_models in enumerate([layer1_models, layer2_models]):
        for model_section in parser.get_layer_models(1):
            method, setting = parser.get_model_setting(model_section)

            if "class_weight" in setting:
                if isinstance(setting["class_weight"], int):
                    setting["class_weight"] = {0: setting["class_weight"], 1: 1}
                else:
                    try:
                        setting["class_weight"] = {0: float(setting["class_weight"]), 1: 1}
                    except:
                        setting["class_weight"] = "balanced"

            if method.find("deep") > -1:
                setting["folder"] = None

                if setting["data_dimension"] == "basic":
                    setting["input_dims"] = len(basic_columns)
                elif setting["data_dimension"] == "importance":
                    setting["input_dims"] = len(importance_columns)
                elif setting["data_dimension"].find("interaction-information") != -1:
                    setting["input_dims"] = top_feature
                else:
                    log("Wrong Setting for input_dims because the data_dimension is {}".format(setting["data_dimension"]), ERRPR)
                    sys.exit(100)

                setting["callbacks"] = [checkpointer]
                setting["number_of_layer"] = setting.pop("layer_number")

            layer_models.append((method, setting))
            log("Get the configuration of {} from {}".format(method, conf), INFO)
            log("The setting is {}".format(setting), INFO)

    for model_section in parser.get_layer_models(3):
        method, setting = parser.get_model_setting(model_section)

        last_model.append((method, setting))

    folder_model = "{}/prediction_model/ensemble_learning/is_testing={}_is_feature_importance={}_nfold={}_layer1={}_layer2={}_binsize={}_top={}".format(\
                        BASEPATH, is_testing, is_feature_importance, nfold, len(layer1_models), len(layer2_models), binsize, top)

    folder_middle = "{}/etc/middle_layer/is_testing={}_is_feature_importance={}_nfold={}_binsize={}_top={}".format(\
                        BASEPATH, is_testing, is_feature_importance, nfold, binsize, top)

    if is_testing:
        log("Due to the testing mode, remove the {} firstly".format(folder_model), INFO)
        shutil.rmtree(folder_model)

    if not os.path.exists(folder_model):
        os.makedirs(folder_model)

    filepath_training = "{}/training_proba_tracking.csv".format(folder_model)
    filepath_testing = "{}/testing_proba_tracking.csv".format(folder_model)

    # Phase 1. --> Model Training
    filepath_queue = "{}/layer1_queue.pkl".format(folder_model)
    filepath_nfold = "{}/layer1_nfold.pkl".format(folder_model)
    layer2_train_x, layer2_test_x, learning_loss = layer_model(\
                             objective, folder_model, folder_middle, predictors, train_x, train_Y, test_x, layer1_models,
                             filepath_queue, filepath_nfold,
                             n_folds=nfold, cost_string=cost, number_of_thread=thread, saving_results=True)

    # Phase 2. --> Model Training
    filepath_queue = "{}/layer2_queue.pkl".format(folder_model)
    filepath_nfold = "{}/layer2_nfold.pkl".format(folder_model)
    layer3_train_x, layer3_test_x, learning_loss = layer_model(\
                             objective, folder_model, folder_middle, None, layer2_train_x, train_Y, layer2_test_x, layer2_models,
                             filepath_queue, filepath_nfold,
                             n_folds=nfold, cost_string=cost, number_of_thread=thread, saving_results=False)

    training_dataset_proba = np.hstack((layer2_train_x, layer3_train_x))
    training_targets = [{"Target": train_y}]
    store_layer_output([m[0] for m in layer1_models+layer2_models], training_dataset_proba, filepath_training, optional=training_targets)

    testing_dataset_proba = np.hstack((layer2_test_x, layer3_test_x))
    testing_targets = [{"ID": test_id}]
    store_layer_output([m[0] for m in layer1_models+layer2_models], testing_dataset_proba, filepath_testing, optional=testing_targets)

    # Phase 3. --> Model Training
    submission_results = final_model(objective, last_model[0], layer3_train_x, train_Y, layer3_test_x, cost_string=cost)

if __name__ == "__main__":
    learning()
