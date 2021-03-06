#!/usr/bin/env python

import click
import numpy as np
import pandas as pd

import feature_engineering

from utils import create_folder, log, INFO, WARN, ERROR
from load import load_data, data_transform_2, load_interaction_information, save_cache, load_cache
from parameter_tuning import tuning
from feature_engineering import pca
from configuration import ModelConfParser

@click.command()
@click.option("--conf", required=True, help="Filepath of Configuration")
@click.option("--n-jobs", default=1, help="Number of thread")
@click.option("--is-pca", is_flag=True, help="Turn on the PCA mode")
@click.option("--is-testing", is_flag=True, help="Testing mode")
@click.option("--methodology", required=True, help="Tune parameters of which methodology")
@click.option("--nfold", default=3, help="the number of nfold")
@click.option("--n-estimator", default=200, help="the number of estimator")
def parameter_tuning(methodology, nfold, is_pca, is_testing, n_jobs, conf, n_estimator):
    drop_fields = []

    parser = ModelConfParser(conf)

    objective = parser.get_objective()
    cost = parser.get_cost()

    filepath_training, filepath_testing, filepath_submission, filepath_tuning = parser.get_filepaths(methodology)
    filepath_feature_importance, top = parser.get_feature_importance()
    filepath_feature_interaction, binsize, top_feature = parser.get_feature_interaction()

    for filepath in [filepath_tuning, filepath_submission]:
        create_folder(filepath)

    filepath_cache_1 = "{}/input/train.pkl".format(BASEPATH)
    train_x, test_x, train_y, test_id, _ = load_data(filepath_cache_1, filepath_training, filepath_testing, drop_fields)

    pool = []
    for value, count in zip(values, counts):
        if count > 2:
            pool.append(value)

    idxs = train_y.isin(pool)

    train_x = train_x[idxs].values
    train_y = train_y[idxs].astype(str).values

    test_x = test_x.values
    test_id = df_testing["row_id"].values

    if filepath_feature_interaction:
        for layers, value in load_interaction_information(filepath_feature_interaction, str(top_feature)):
            for df in [train_x, test_x]:
                t = value
                breaking_layer = None
                for layer in layers:
                    if layer in train_x.columns:
                        t *= df[layer]
                    else:
                        breaking_layer = layer
                        break

                if breaking_layer == None:
                    df[";".join(layers)] = t
                else:
                    log("Skip {}".format(layers), WARN)
                    break

    if is_pca:
        train_x, test_x = pca(train_x, train_y.values, test_x)

    if is_testing:
        train_x = train_x.head(1000)
        train_y = train_y.head(1000)

    params = tuning(train_x, train_y, test_id, test_x, cost, objective,
                    filepath_feature_importance, filepath_tuning, filepath_submission, methodology, nfold, top_feature,
                    n_estimator=n_estimator, thread=n_jobs)

    log("The final parameters are {}".format(params))

if __name__ == "__main__":
    parameter_tuning()
