import ray
from ray.rllib.utils.memory import ray_get_and_free   # Force memory deallocation
import numpy as np
import logging
from mlopt.problem import solve_with_strategy
from mlopt.strategy import strategy_distance
import mlopt.settings as stg
from mlopt.problem import Problem
from tqdm import tqdm
import os


@ray.remote
def best_strategy_ray(*args):
    """Ray wrapper."""
    return best_strategy(*args)


def best_strategy(theta, obj_train, encoding, problem):
    """Compute best strategy between the ones in encoding."""

    problem.populate(theta)  # Populate parameters

    # Serial solution over the strategies
    results = [solve_with_strategy(problem, strategy)
               for strategy in encoding]

    # Compute cost degradation
    degradation = []
    for r in results:
        diff = np.abs(r['cost'] - obj_train)
        if np.abs(obj_train) > stg.DIVISION_TOL:  # Normalize in case
            diff /= np.abs(obj_train)
        degradation.append(diff)

    # Find minimum one
    best_strategy = np.argmin(degradation)
    if degradation[best_strategy] > stg.FILTER_SUBOPT:
        logging.warning("Sample assigned to strategy more " +
                        "than %.2e suboptimal." % stg.FILTER_SUBOPT)

    return best_strategy, degradation[best_strategy]


class Filter(object):
    """Strategy filter."""
    def __init__(self,
                 X_train=None,
                 y_train=None,
                 obj_train=None,
                 encoding=None,
                 problem=None):
        """Initialize strategy condenser."""
        self.X_train = X_train
        self.y_train = y_train
        self.encoding = encoding
        self.obj_train = obj_train
        self.problem = problem

    def assign_samples(self, discarded_samples, selected_strategies,
                       parallel=True):
        """
        Assign samples to strategies choosing the ones minimizing the cost.
        """

        # Backup strategies labels and encodings
        self.y_full = self.y_train

        # Reassign y_labels
        # selected_strategies: find index where new labels are
        # discarded_strategies: -1
        self.y_train = np.array([np.where(selected_strategies == label)[0][0]
                                 if label in selected_strategies
                                 else -1
                                 for label in self.y_train])

        # Assign discarded samples and compute degradation
        degradation = np.zeros(len(discarded_samples))

        if not parallel:

            for i in tqdm(range(len(discarded_samples))):
                sample_idx = discarded_samples[i]
                self.y_train[sample_idx], degradation[i] = \
                    best_strategy(self.X_train.iloc[sample_idx],
                                  self.obj_train[sample_idx],
                                  self.encoding,
                                  self.problem)

        else:
            # Share encoding between all processors
            encoding_id = ray.put(self.encoding)

            result_ids = []
            for i in discarded_samples:
                result_ids.append(
                    best_strategy_ray.remote(self.X_train.iloc[i],
                                             self.obj_train[i],
                                             encoding_id,
                                             self.problem))

            for i in tqdm(range(len(discarded_samples))):
                self.y_train[discarded_samples[i]], degradation[i] = \
                    ray_get_and_free(result_ids[i])

        return degradation

    def select_strategies(self, samples_fraction):
        """Select the most frequent strategies depending on the counts"""

        n_samples = len(self.X_train)
        n_strategies = len(self.encoding)
        n_samples_selected = int(samples_fraction * n_samples)

        logging.info("Selecting most frequent strategies")

        # Select strategies with high frequency counts
        strategies, y_counts = np.unique(self.y_train, return_counts=True)
        assert n_strategies == len(strategies)  # Sanity check

        # Sort from largest to smallest counts and pick
        # only the first ones covering up to samples_fraction samples
        idx_sort = np.argsort(y_counts)[::-1]
        selected_strategies = []
        n_temp = 0
        for idx in idx_sort:
            n_temp += y_counts[idx]  # count selected samples
            selected_strategies.append(strategies[idx])
            if n_temp > n_samples_selected:
                break

        logging.info("Selected %d strategies" % len(selected_strategies))

        return selected_strategies

    def filter(self,
               samples_fraction=stg.FILTER_STRATEGIES_SAMPLES_FRACTION,
               parallel=True):
        """Filter strategies."""
        n_samples = len(self.X_train)

        # Backup strategies labels and encodings
        self.y_full = self.y_train
        self.encoding_full = self.encoding

        selected_strategies = \
            self.select_strategies(samples_fraction=samples_fraction)

        logging.info("Number of chosen strategies %d" %
                     len(selected_strategies))

        # Reassign encodings and labels
        self.encoding = [self.encoding[i] for i in selected_strategies]

        # Find discarded samples
        discarded_samples = np.array([i for i in range(n_samples)
                                      if self.y_train[i]
                                      not in selected_strategies])

        logging.info("Discarded strategies for %d samples (%.2f %%)" %
                     (len(discarded_samples),
                      (100 * len(discarded_samples) / n_samples)))

        logging.info("Reassign samples with discarded strategies")

        # Reassign discarded samples to selected strategies
        degradation = self.assign_samples(discarded_samples,
                                          selected_strategies,
                                          parallel=parallel)

        logging.info("Average cost degradation = %.2e %%" %
                     (100 * np.mean(degradation)))
        logging.info("Max cost degradation = %.2e %%" %
                     (100 * np.max(degradation)))

        return self.y_train, self.encoding