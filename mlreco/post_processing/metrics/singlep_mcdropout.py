import numpy as np
import pandas as pd
import sys, os, re

from mlreco.post_processing import post_processing
from mlreco.utils import CSVData

from scipy.special import softmax as softmax_func
from scipy.stats import entropy

import torch

def singlep_mcdropout(cfg, processor_cfg, data_blob, result, logdir, iteration):

    labels = data_blob['label'][0][:, 0]
    index = data_blob['index']
    # logits = result['logits'][0]
    if processor_cfg['mode'] != 'mcdropout':
        softmax = softmax_func(result['logits'][0], axis=1)
        mc_dist = result['logits'][0]
    else:
        softmax = result['softmax'][0]
        mc_dist = result['mc_dist'][0]

    pred = np.argmax(softmax, axis=1)
    index = np.asarray(index)

    if iteration:
        append = True
    else:
        append = False

    fout = CSVData(
        os.path.join(logdir, 'singlep-mcdropout-metrics.csv'), append=append)

    for batch_id, event_id in enumerate(index):

        probs = softmax[batch_id]
        mc_probs = mc_dist[batch_id]
        pred = np.argmax(probs)
        label_batch = labels[batch_id]
        ent = entropy(probs)

        fout.record(('Index', 'Truth', 'Prediction', 
                    'p0', 'p1', 'p2', 'p3', 'p4', 
                    'mc_p0', 'mc_p1', 'mc_p2', 'mc_p3', 'mc_p4', 'entropy'),
                    (int(event_id), int(label_batch), int(pred),
                     probs[0], probs[1], probs[2], probs[3], probs[4], 
                     mc_probs[0], mc_probs[1], mc_probs[2], mc_probs[3], mc_probs[4], ent))
        fout.write()

    fout.close()


def singlep_mcdropout_loop(cfg, processor_cfg, data_blob, result, logdir, iteration):

    labels = data_blob['label'][0][:, 0]
    index = data_blob['index']
    # logits = result['logits'][0]
    if processor_cfg['mode'] != 'mcdropout':
        softmax = softmax_func(result['logits'][0], axis=1)
        mc_dist = result['logits'][0]
    else:
        softmax = result['softmax'][0]
        mc_dist = result['mc_dist'][0]

    pred = np.argmax(softmax, axis=1)
    index = np.asarray(index)

    if iteration:
        append = True
    else:
        append = False

    fout = CSVData(
        os.path.join(logdir, 'singlep-mcdropout-metrics.csv'), append=append)

    for batch_id, event_id in enumerate(index):

        probs = softmax[batch_id]
        mc_probs = mc_dist[batch_id]
        pred = np.argmax(probs)
        label_batch = labels[batch_id]
        ent = entropy(probs)

        fout.record(('Index', 'Truth', 'Prediction', 
                    'p0', 'p1', 'p2', 'p3', 'p4', 
                    'mc_p0', 'mc_p1', 'mc_p2', 'mc_p3', 'mc_p4', 'entropy'),
                    (int(event_id), int(label_batch), int(pred),
                     probs[0], probs[1], probs[2], probs[3], probs[4], 
                     mc_probs[0], mc_probs[1], mc_probs[2], mc_probs[3], mc_probs[4], ent))
        fout.write()

    fout.close()