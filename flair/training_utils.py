import logging
import random
import sys
from collections import defaultdict
from enum import Enum
from functools import reduce
from math import inf
from pathlib import Path
from typing import Any, Dict, List, Optional, Type, Union

try:
    from typing import Literal
except ImportError:
    from typing_extension import Literal  # type: ignore

import torch
import torchmetrics
from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import mean_absolute_error, mean_squared_error
from torch.optim import Optimizer
from torch.utils.data import Dataset
from torchmetrics.classification.stat_scores import (
    MulticlassStatScores,
    MultilabelStatScores,
)

import flair
from flair.data import DT, Dictionary, Sentence, _iter_dataset

log = logging.getLogger("flair")


class Result(object):
    def __init__(
        self,
        main_score: float,
        detailed_results: str,
        classification_report: dict = {},
        scores: dict = {},
    ):
        assert "loss" in scores, "No loss provided."

        self.main_score: float = main_score
        self.scores = scores
        self.detailed_results: str = detailed_results
        self.classification_report = classification_report

    @property
    def loss(self):
        return self.scores["loss"]

    def __str__(self):
        return f"{str(self.detailed_results)}\nLoss: {self.loss}'"


class MetricRegression(object):
    def __init__(self, name):
        self.name = name

        self.true = []
        self.pred = []

    def mean_squared_error(self):
        return mean_squared_error(self.true, self.pred)

    def mean_absolute_error(self):
        return mean_absolute_error(self.true, self.pred)

    def pearsonr(self):
        return pearsonr(self.true, self.pred)[0]

    def spearmanr(self):
        return spearmanr(self.true, self.pred)[0]

    # dummy return to fulfill trainer.train() needs
    def micro_avg_f_score(self):
        return self.mean_squared_error()

    def to_tsv(self):
        return "{}\t{}\t{}\t{}".format(
            self.mean_squared_error(),
            self.mean_absolute_error(),
            self.pearsonr(),
            self.spearmanr(),
        )

    @staticmethod
    def tsv_header(prefix=None):
        if prefix:
            return "{0}_MEAN_SQUARED_ERROR\t{0}_MEAN_ABSOLUTE_ERROR\t{0}_PEARSON\t{0}_SPEARMAN".format(prefix)

        return "MEAN_SQUARED_ERROR\tMEAN_ABSOLUTE_ERROR\tPEARSON\tSPEARMAN"

    @staticmethod
    def to_empty_tsv():
        return "\t_\t_\t_\t_"

    def __str__(self):
        line = (
            "mean squared error: {0:.4f} - mean absolute error: {1:.4f} - pearson: {2:.4f} - spearman: {3:.4f}".format(
                self.mean_squared_error(),
                self.mean_absolute_error(),
                self.pearsonr(),
                self.spearmanr(),
            )
        )
        return line


class EvaluationMetric(Enum):
    MICRO_ACCURACY = "micro-average accuracy"
    MICRO_F1_SCORE = "micro-average f1-score"
    MACRO_ACCURACY = "macro-average accuracy"
    MACRO_F1_SCORE = "macro-average f1-score"
    MEAN_SQUARED_ERROR = "mean squared error"


class WeightExtractor(object):
    def __init__(self, directory: Union[str, Path], number_of_weights: int = 10):
        if type(directory) is str:
            directory = Path(directory)
        self.weights_file = init_output_file(directory, "weights.txt")
        self.weights_dict: Dict[str, Dict[int, List[float]]] = defaultdict(lambda: defaultdict(lambda: list()))
        self.number_of_weights = number_of_weights

    def extract_weights(self, state_dict, iteration):
        for key in state_dict.keys():
            vec = state_dict[key]
            # print(vec)
            try:
                weights_to_watch = min(self.number_of_weights, reduce(lambda x, y: x * y, list(vec.size())))
            except Exception:
                continue

            if key not in self.weights_dict:
                self._init_weights_index(key, state_dict, weights_to_watch)

            for i in range(weights_to_watch):
                vec = state_dict[key]
                for index in self.weights_dict[key][i]:
                    vec = vec[index]

                value = vec.item()

                with open(self.weights_file, "a") as f:
                    f.write("{}\t{}\t{}\t{}\n".format(iteration, key, i, float(value)))

    def _init_weights_index(self, key, state_dict, weights_to_watch):
        indices = {}

        i = 0
        while len(indices) < weights_to_watch:
            vec = state_dict[key]
            cur_indices = []

            for x in range(len(vec.size())):
                index = random.randint(0, len(vec) - 1)
                vec = vec[index]
                cur_indices.append(index)

            if cur_indices not in list(indices.values()):
                indices[i] = cur_indices
                i += 1

        self.weights_dict[key] = indices


class AnnealOnPlateau(object):
    """A learningrate sheduler for annealing on plateau.

    This class is a modification of
    torch.optim.lr_scheduler.ReduceLROnPlateau that enables
    setting an "auxiliary metric" to break ties.
    Reduce learning rate when a metric has stopped improving.
    Models often benefit from reducing the learning rate by a factor
    of 2-10 once learning stagnates. This scheduler reads a metrics
    quantity and if no improvement is seen for a 'patience' number
    of epochs, the learning rate is reduced.

    Args:
        optimizer (Optimizer): Wrapped optimizer.
        mode (str): One of `min`, `max`. In `min` mode, lr will
            be reduced when the quantity monitored has stopped
            decreasing; in `max` mode it will be reduced when the
            quantity monitored has stopped increasing. Default: 'min'.
        factor (float): Factor by which the learning rate will be
            reduced. new_lr = lr * factor. Default: 0.1.
        patience (int): Number of epochs with no improvement after
            which learning rate will be reduced. For example, if
            `patience = 2`, then we will ignore the first 2 epochs
            with no improvement, and will only decrease the LR after the
            3rd epoch if the loss still hasn't improved then.
            Default: 10.
        verbose (bool): If ``True``, prints a message to stdout for
            each update. Default: ``False``.
        cooldown (int): Number of epochs to wait before resuming
            normal operation after lr has been reduced. Default: 0.
        min_lr (float or list): A scalar or a list of scalars. A
            lower bound on the learning rate of all param groups
            or each group respectively. Default: 0.
        eps (float): Minimal decay applied to lr. If the difference
            between new and old lr is smaller than eps, the update is
            ignored. Default: 1e-8.

    Example:
        >>> optimizer = torch.optim.SGD(model.parameters(), lr=0.1, momentum=0.9)
        >>> scheduler = ReduceLROnPlateau(optimizer, 'min')
        >>> for epoch in range(10):
        >>>     train(...)
        >>>     val_loss = validate(...)
        >>>     # Note that step should be called after validate()
        >>>     scheduler.step(val_loss)
    """

    def __init__(
        self,
        optimizer,
        mode="min",
        aux_mode="min",
        factor=0.1,
        patience=10,
        initial_extra_patience=0,
        verbose=False,
        cooldown=0,
        min_lr=0,
        eps=1e-8,
    ):
        if factor >= 1.0:
            raise ValueError("Factor should be < 1.0.")
        self.factor = factor

        # Attach optimizer
        if not isinstance(optimizer, Optimizer):
            raise TypeError("{} is not an Optimizer".format(type(optimizer).__name__))
        self.optimizer = optimizer

        if isinstance(min_lr, list) or isinstance(min_lr, tuple):
            if len(min_lr) != len(optimizer.param_groups):
                raise ValueError("expected {} min_lrs, got {}".format(len(optimizer.param_groups), len(min_lr)))
            self.min_lrs = list(min_lr)
        else:
            self.min_lrs = [min_lr] * len(optimizer.param_groups)

        self.default_patience = patience
        self.effective_patience = patience + initial_extra_patience
        self.verbose = verbose
        self.cooldown = cooldown
        self.cooldown_counter = 0
        self.mode = mode
        self.aux_mode = aux_mode
        self.best = None
        self.best_aux = None
        self.num_bad_epochs = None
        self.mode_worse = None  # the worse value for the chosen mode
        self.eps = eps
        self.last_epoch = 0
        self._init_is_better(mode=mode)
        self._reset()

    def _reset(self):
        """Resets num_bad_epochs counter and cooldown counter."""
        self.best = self.mode_worse
        self.cooldown_counter = 0
        self.num_bad_epochs = 0

    def step(self, metric, auxiliary_metric=None) -> bool:
        # convert `metrics` to float, in case it's a zero-dim Tensor
        current = float(metric)
        epoch = self.last_epoch + 1
        self.last_epoch = epoch

        is_better = False

        if self.mode == "min":
            if current < self.best:
                is_better = True

        if self.mode == "max":
            if current > self.best:
                is_better = True

        if current == self.best and auxiliary_metric:
            current_aux = float(auxiliary_metric)
            if self.aux_mode == "min":
                if current_aux < self.best_aux:
                    is_better = True

            if self.aux_mode == "max":
                if current_aux > self.best_aux:
                    is_better = True

        if is_better:
            self.best = current
            if auxiliary_metric:
                self.best_aux = auxiliary_metric
            self.num_bad_epochs = 0
        else:
            self.num_bad_epochs += 1

        if self.in_cooldown:
            self.cooldown_counter -= 1
            self.num_bad_epochs = 0  # ignore any bad epochs in cooldown

        reduce_learning_rate = True if self.num_bad_epochs > self.effective_patience else False
        if reduce_learning_rate:
            self._reduce_lr(epoch)
            self.cooldown_counter = self.cooldown
            self.num_bad_epochs = 0
            self.effective_patience = self.default_patience

        self._last_lr = [group["lr"] for group in self.optimizer.param_groups]

        return reduce_learning_rate

    def _reduce_lr(self, epoch):
        for i, param_group in enumerate(self.optimizer.param_groups):
            old_lr = float(param_group["lr"])
            new_lr = max(old_lr * self.factor, self.min_lrs[i])
            if old_lr - new_lr > self.eps:
                param_group["lr"] = new_lr
                if self.verbose:
                    log.info(f" - reducing learning rate of group {epoch} to {new_lr}")

    @property
    def in_cooldown(self):
        return self.cooldown_counter > 0

    def _init_is_better(self, mode):
        if mode not in {"min", "max"}:
            raise ValueError("mode " + mode + " is unknown!")

        if mode == "min":
            self.mode_worse = inf
        else:  # mode == 'max':
            self.mode_worse = -inf

        self.mode = mode

    def state_dict(self):
        return {key: value for key, value in self.__dict__.items() if key != "optimizer"}

    def load_state_dict(self, state_dict):
        self.__dict__.update(state_dict)
        self._init_is_better(mode=self.mode)


def init_output_file(base_path: Union[str, Path], file_name: str) -> Path:
    """Creates a local file which can be appended to.

    :param base_path: the path to the directory
    :param file_name: the file name
    :return: the created file
    """
    base_path = Path(base_path)
    base_path.mkdir(parents=True, exist_ok=True)

    file = base_path / file_name
    open(file, "w", encoding="utf-8").close()
    return file


def convert_labels_to_one_hot(label_list: List[List[str]], label_dict: Dictionary) -> List[List[int]]:
    """Convert list of labels to a one hot list.

    :param label_list: list of labels
    :param label_dict: label dictionary
    :return: converted label list
    """
    return [[1 if label in labels else 0 for label in label_dict.get_items()] for labels in label_list]


def log_line(log):
    if sys.version_info >= (3, 8):
        log.info("-" * 100, stacklevel=3)
    else:
        log.info("-" * 100)


def add_file_handler(log, output_file):
    init_output_file(output_file.parents[0], output_file.name)
    fh = logging.FileHandler(output_file, mode="w", encoding="utf-8")
    fh.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)-15s %(message)s")
    fh.setFormatter(formatter)
    log.addHandler(fh)
    return fh


def store_embeddings(
    data_points: Union[List[DT], Dataset], storage_mode: str, dynamic_embeddings: Optional[List[str]] = None
):
    if isinstance(data_points, Dataset):
        data_points = list(_iter_dataset(data_points))

    # if memory mode option 'none' delete everything
    if storage_mode == "none":
        dynamic_embeddings = None

    # if dynamic embedding keys not passed, identify them automatically
    elif dynamic_embeddings is None:
        dynamic_embeddings = identify_dynamic_embeddings(data_points)

    # always delete dynamic embeddings
    for data_point in data_points:
        data_point.clear_embeddings(dynamic_embeddings)

    # if storage mode is "cpu", send everything to CPU (pin to memory if we train on GPU)
    if storage_mode == "cpu":
        pin_memory = str(flair.device) != "cpu"
        for data_point in data_points:
            data_point.to("cpu", pin_memory=pin_memory)


def identify_dynamic_embeddings(data_points: List[DT]):
    dynamic_embeddings = []
    all_embeddings = []
    for data_point in data_points:
        if isinstance(data_point, Sentence):
            first_token = data_point[0]
            for name, vector in first_token._embeddings.items():
                if vector.requires_grad:
                    dynamic_embeddings.append(name)
                all_embeddings.append(name)

        for name, vector in data_point._embeddings.items():
            if vector.requires_grad:
                dynamic_embeddings.append(name)
            all_embeddings.append(name)
        if dynamic_embeddings:
            return dynamic_embeddings
    if not all_embeddings:
        return None
    return list(set(dynamic_embeddings))


class BinarySupportMetric(torchmetrics.classification.BinaryStatScores):
    is_differentiable = False
    full_state_update: bool = False

    def compute(self) -> torch.Tensor:
        """Computes accuracy based on inputs passed in to ``update`` previously."""
        return super().compute()[..., -1]


class MutliclassSupportMetric(torchmetrics.classification.MulticlassStatScores):
    is_differentiable = False
    full_state_update: bool = False

    def compute(self) -> torch.Tensor:
        """Computes accuracy based on inputs passed in to ``update`` previously."""
        return super().compute()[..., -1]


class MultilabelSupportMetric(torchmetrics.classification.MultilabelStatScores):
    is_differentiable = False
    full_state_update: bool = False

    def compute(self) -> torch.Tensor:
        """Computes accuracy based on inputs passed in to ``update`` previously."""
        return super().compute()[..., -1]


class SupportMetric:
    def __new__(
        cls,
        task: Literal["binary", "multiclass", "multilabel"],
        threshold: float = 0.5,
        num_classes: Optional[int] = None,
        num_labels: Optional[int] = None,
        average: Optional[Literal["micro", "macro", "weighted", "none"]] = "micro",
        multidim_average: Literal["global", "samplewise"] = "global",
        top_k: Optional[int] = 1,
        ignore_index: Optional[int] = None,
        validate_args: bool = True,
        **kwargs: Any,
    ):
        kwargs.update(dict(multidim_average=multidim_average, ignore_index=ignore_index, validate_args=validate_args))
        if task == "binary":
            return BinarySupportMetric(threshold, **kwargs)
        if task == "multiclass":
            assert isinstance(num_classes, int)
            assert isinstance(top_k, int)
            return MutliclassSupportMetric(num_classes, top_k, average, **kwargs)
        if task == "multilabel":
            assert isinstance(num_labels, int)
            return MultilabelSupportMetric(num_labels, threshold, average, **kwargs)
        raise ValueError(
            f"Expected argument `task` to either be `'binary'`, `'multiclass'` or `'multilabel'` but got {task}"
        )


def metric_with_certain_labels_only(
    metric_type: Union[Type[MulticlassStatScores], Type[MultilabelStatScores]],
    included_labels: torch.Tensor,
    average: Optional[Literal["micro", "macro", "weighted", "none"]] = "macro",
    **kwargs,
):
    metric_average = average

    if average == "micro":
        metric_average = "none"

    metric = metric_type(average=metric_average, **kwargs)

    _final_state_inner = metric._final_state

    def _final_state_wrapper():
        state = _final_state_inner()

        # manipulate the state variable
        new_state = (s[torch.tensor(included_labels)] for s in state)

        return new_state

    metric._final_state = _final_state_wrapper  # type: ignore

    if average == "micro":
        compute_inner = metric.compute

        def compute_wrapper():
            metric.average = "micro"
            result = compute_inner()
            metric.average = "none"
            return result

        metric.compute = compute_wrapper  # type: ignore

    return metric


class LabelwiseWrapper(torchmetrics.ClasswiseWrapper):
    def __init__(self, metric, name, label_names, included_labels, **kwargs):
        super().__init__(metric, labels=label_names, **kwargs)

        self._name = name
        self.included_labels = included_labels

    def _convert(self, x: torch.Tensor) -> Dict[str, Any]:
        x = x[torch.tensor(self.included_labels)]

        name = self._name
        if self.labels is None:
            return {f"label_{i}_{name}": val for i, val in enumerate(x)}
        return {f"label_{lab}_{name}": val for lab, val in zip(self.labels, x)}
