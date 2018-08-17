import os
import logging
from time import strftime
from spwsi.spwsi import DEFAULT_PARAMS, SPWSI
import multiprocessing
import numpy as np
from tqdm import tqdm
import time
import csv

# will be set per process during worker init and persist until the end
spwsi_runner = None

# Changing these requires a restart
LM_BATCH_SIZE = 20
LM_VOACB_CUTOFF = 50000

DEBUG_DIR = 'debug'
run_name = ''  # set in main

cuda_device_dispatcher = None  # set in main

gpus = [0, 0, 1, 1, 2, 2, 3, 3]  # multiple workers per gpu


# generate configurations to run
def get_configs_ablations():
    """
    generates 10 of each ablation scenario
    """
    for _ in range(10):
        yield dict()
        yield dict(disable_symmetric_patterns=True)
        yield dict(disable_lemmatization=True)
        yield dict(disable_tfidf=True)
        yield dict(disable_symmetric_patterns=True, disable_lemmatization=True)
        yield dict(disable_symmetric_patterns=True, disable_lemmatization=True, disable_tfidf=True)


def get_configs_random_search():
    """
    used to validate our parameters are sane and abaltion results are consistent

     we put some prior on the default params which seem good,
     keeping default values according to a coin flip
    """

    def random_log_uni_int(low, high):
        low = np.log(low)
        high = np.log(high)
        return int(np.exp(np.random.uniform(low, high)))

    def flip_coin():
        return np.random.choice([True, False])

    while True:
        proposed_conf = DEFAULT_PARAMS.copy()
        method = np.random.choice(['no-disable', 'nosp', 'rand', 'none'])
        if method == 'nosp':
            proposed_conf['disable_symmetric_patterns'] = True
            proposed_conf['disable_tfidf'] = False
            proposed_conf['disable_lemmatization'] = False
        elif method == 'rand':
            proposed_conf['disable_symmetric_patterns'] = flip_coin()
            proposed_conf['disable_tfidf'] = flip_coin()
            proposed_conf['disable_lemmatization'] = flip_coin()
        elif method == 'none':
            proposed_conf['disable_symmetric_patterns'] = True
            proposed_conf['disable_tfidf'] = True
            proposed_conf['disable_lemmatization'] = True
        else:
            proposed_conf['disable_symmetric_patterns'] = False
            proposed_conf['disable_tfidf'] = False
            proposed_conf['disable_lemmatization'] = False

        if flip_coin():
            proposed_conf['prediction_cutoff'] = random_log_uni_int(20, 1000)
        if flip_coin():
            proposed_conf['n_clusters'] = np.random.randint(5, 10)
        if flip_coin():
            proposed_conf['n_represent'] = random_log_uni_int(4, 100)
        if flip_coin():
            proposed_conf['n_samples_side'] = np.random.randint(5, 10)
        yield proposed_conf


def worker_init():
    global spwsi_runner, LM_BATCH_SIZE, LM_VOACB_CUTOFF, cuda_device_dispatcher
    from spwsi.bilm_elmo import BilmElmo
    worker_id, cuda_device = cuda_device_dispatcher.get()
    np.random.seed((int(time.time() * 100) % 10000) + worker_id)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    handler = logging.FileHandler(os.path.join(debug_dir, '%s.worker.%d.log.txt' % (run_name, cuda_device)), 'w',
                                  'utf-8')
    formatter = logging.Formatter(fmt='%(asctime)s %(message)s', datefmt='%H:%M:%S')
    handler.setFormatter(formatter)  # Pass handler as a parameter, not assign
    root_logger.addHandler(handler)

    elmo_vocab_path = './resources/vocab-2016-09-10.txt'
    BilmElmo.create_lemmatized_vocabulary_if_needed(elmo_vocab_path)
    elmo_as_lm = BilmElmo(cuda_device, './resources/elmo_2x4096_512_2048cnn_2xhighway_softmax_weights.hdf5',
                          elmo_vocab_path, batch_size=LM_BATCH_SIZE,
                          cutoff_elmo_vocab=LM_VOACB_CUTOFF
                          )
    logging.info('created ELMo on cuda device %d' % cuda_device)
    spwsi_runner = SPWSI(elmo_as_lm)


def worker_do(idx_conf):
    idx, worker_conf = idx_conf
    global spwsi_runner, run_name
    run_name_full = '%s.%d' % (run_name, idx)
    params = DEFAULT_PARAMS.copy()
    params.update(worker_conf)
    logging.info('running with config %s' % params)
    res = spwsi_runner.run(n_clusters=params['n_clusters'],
                           n_represent=params['n_represent'],
                           n_samples_side=params['n_samples_side'],
                           disable_tfidf=params['disable_tfidf'],
                           disable_lemmatization=params['disable_lemmatization'],
                           disable_symmetric_patterns=params['disable_symmetric_patterns'],
                           prediction_cutoff=params['prediction_cutoff'],
                           run_name=run_name_full,
                           debug_dir=DEBUG_DIR,
                           print_progress=False)
    return run_name_full, params, res


if __name__ == '__main__':
    print('BiLM Symmetric Patterns WSI Demo - Ablation')

    cuda_device_dispatcher = multiprocessing.Queue()
    for i, gpu in enumerate(gpus):
        cuda_device_dispatcher.put((i, gpu))

    debug_dir = 'debug'

    run_name = 'batch-' + strftime("%m%d-%H%M%S")
    if not os.path.exists(debug_dir):
        os.makedirs(debug_dir)

    pool = multiprocessing.Pool(len(gpus), initializer=worker_init)

    out_csv_path = os.path.join(debug_dir, run_name + '.data.csv')
    print('starting ablation tests. results will be written to %s. this might take a while...' % out_csv_path)
    # in addition to per target scores, an "all" entry row will contain the final result for a run
    with open(out_csv_path, 'a') as fout:
        writer = csv.writer(fout)
        conf_params_report = ['n_clusters', 'n_represent', 'n_samples_side', 'disable_lemmatization',
                              'disable_symmetric_patterns', 'disable_tfidf', 'prediction_cutoff']
        writer.writerow(
            ['run_name', 'target', 'FBC', 'FNMI', 'AVG', 'lm_batch_size', 'cutoff_lm_vocab'] + conf_params_report)
        for run_name_done, conf, scores in tqdm(pool.imap_unordered(worker_do, enumerate(get_configs_random_search()))):
            for target, target_scores in scores.items():
                writer.writerow([run_name_done, target,
                                 target_scores['FBC'],
                                 target_scores['FNMI'],
                                 np.sqrt(target_scores['FBC'] * target_scores['FNMI']),
                                 LM_BATCH_SIZE,
                                 LM_VOACB_CUTOFF,
                                 ] + [conf[x] for x in conf_params_report])
            fout.flush()
