import logging

import torch
from torch.optim import Optimizer
from torch.optim.lr_scheduler import LambdaLR, ReduceLROnPlateau, _LRScheduler
from torch.optim.optimizer import required

log = logging.getLogger("flair")


class SGDW(Optimizer):
    r"""Implements stochastic gradient descent (optionally with momentum) with weight decay.

    Implementation from the paper `Fixing Weight Decay Regularization in Adam`_.
    Nesterov momentum is based on the formula from
    `On the importance of initialization and momentum in deep learning`__.

    Args:
    ----
        params (iterable): iterable of parameters to optimize or dicts defining
            parameter groups
        lr (float): learning rate
        momentum (float, optional): momentum factor (default: 0)
        weight_decay (float, optional): weight decay factor (default: 0)
        dampening (float, optional): dampening for momentum (default: 0)
        nesterov (bool, optional): enables Nesterov momentum (default: False)

    .. _Fixing Weight Decay Regularization in Adam:
        https://arxiv.org/abs/1711.05101

    Example:
    -------
        >>> optimizer = torch.optim.SGDW(model.parameters(), lr=0.1, momentum=0.9,
                                         weight_decay=1e-5)
        >>> optimizer.zero_grad()
        >>> loss_fn(model(input), target).backward()
        >>> optimizer.step()

    __ http://www.cs.toronto.edu/%7Ehinton/absps/momentum.pdf

    .. note::
        The implementation of SGD with Momentum/Nesterov subtly differs from
        Sutskever et. al. and implementations in some other frameworks.

        Considering the specific case of Momentum, the update can be written as

        .. math::
                  v = \rho * v + g \\
                  p = p - lr * v

        where p, g, v and :math:`\rho` denote the parameters, gradient,
        velocity, and momentum respectively.

        This is in contrast to Sutskever et. al. and
        other frameworks which employ an update of the form

        .. math::
             v = \rho * v + lr * g \\
             p = p - v

        The Nesterov version is analogously modified.
    """

    def __init__(
        self,
        params,
        lr=required,
        momentum=0,
        dampening=0,
        weight_decay=0,
        nesterov=False,
    ) -> None:
        if lr is not required and lr < 0.0:
            raise ValueError(f"Invalid learning rate: {lr}")
        if momentum < 0.0:
            raise ValueError(f"Invalid momentum value: {momentum}")
        if weight_decay < 0.0:
            raise ValueError(f"Invalid weight_decay value: {weight_decay}")

        defaults = {
            "lr": lr,
            "momentum": momentum,
            "dampening": dampening,
            "weight_decay": weight_decay,
            "nesterov": nesterov,
        }
        if nesterov and (momentum <= 0 or dampening != 0):
            raise ValueError("Nesterov momentum requires a momentum and zero dampening")
        super().__init__(params, defaults)

    def __setstate__(self, state):
        super().__setstate__(state)
        for group in self.param_groups:
            group.setdefault("nesterov", False)

    def step(self, closure=None):
        """Performs a single optimization step.

        Parameters
        ----------
        closure (callable, optional): A closure that reevaluates the model
               and returns the loss.

        Returns:
        -------
        loss (float, optional): The loss if closure was set
        """
        loss = None
        if closure is not None:
            loss = closure()

        for group in self.param_groups:
            weight_decay = group["weight_decay"]
            momentum = group["momentum"]
            dampening = group["dampening"]
            nesterov = group["nesterov"]

            for p in group["params"]:
                if p.grad is None:
                    continue
                d_p = p.grad.data

                if momentum != 0:
                    param_state = self.state[p]
                    if "momentum_buffer" not in param_state:
                        buf = param_state["momentum_buffer"] = torch.zeros_like(p.data)
                        buf.mul_(momentum).add_(d_p)
                    else:
                        buf = param_state["momentum_buffer"]
                        buf.mul_(momentum).add_(1 - dampening, d_p)
                    d_p = d_p.add(momentum, buf) if nesterov else buf

                if weight_decay != 0:
                    p.data.add_(-weight_decay, p.data)

                p.data.add_(-group["lr"], d_p)

        return loss


class ExpAnnealLR(_LRScheduler):
    """Exponentially anneal the lr of each parameter group from the initial lr to end_lr over a number of iterations.

    Args:
    ----
        optimizer (Optimizer): Wrapped optimizer.
        end_lr (float): The final learning rate.
        iterations (int): The number of iterations over which to increase the
            learning rate.
        last_epoch (int): The index of the last iteration. Default: -1.
    """

    def __init__(self, optimizer, end_lr, iterations, last_epoch=-1) -> None:
        self.end_lr = end_lr
        self.iterations = iterations
        super().__init__(optimizer, last_epoch=last_epoch)

    def get_lr(self):
        iteration = self.last_epoch + 1
        pct = iteration / self.iterations
        return [base_lr * (self.end_lr / base_lr) ** pct for base_lr in self.base_lrs]


class LinearSchedulerWithWarmup(LambdaLR):
    """Linearly increase the lr from 0 to initial lr during warmup and decrease the lr to 0 after the warmup.

    Uses LambaLR scheduler where the learning rate is multiplied by a lambda factor after calling scheduler.step().

    Args:
    ----
        optimizer (Optimizer): Wrapped optimizer.
        num_train_steps (int): total number of training steps (number of batches * epochs).
        num_warmup_steps (int): number of training steps for learning rate warmup.
        last_epoch (int): The index of the last iteration. Default: -1. The scheduler
            will simply restart when resuming training from a checkpoint.
    """

    def __init__(self, optimizer, num_train_steps, num_warmup_steps, last_epoch=-1) -> None:
        def linear_lr_lambda(current_step: int):
            lambda_during_warmup = float(current_step) / float(max(1, num_warmup_steps))
            lambda_after_warmup = max(
                0.0,
                float(num_train_steps - current_step) / float(max(1, num_train_steps - num_warmup_steps)),
            )
            if current_step < num_warmup_steps:
                return lambda_during_warmup
            return lambda_after_warmup

        super().__init__(optimizer, lr_lambda=linear_lr_lambda, last_epoch=last_epoch)


class ReduceLRWDOnPlateau(ReduceLROnPlateau):
    """Reduce learning rate and weight decay when a metric has stopped improving.

    Models often benefit from reducing the learning rate by
    a factor of 2-10 once learning stagnates. This scheduler reads a metric
    quantity and if no improvement is seen for a 'patience' number
    of epochs, the learning rate and weight decay factor is reduced for
    optimizers that implement the the weight decay method from the paper
    `Fixing Weight Decay Regularization in Adam`_.

    .. _Fixing Weight Decay Regularization in Adam:
        https://arxiv.org/abs/1711.05101

    Args:
    ----
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
        threshold (float): Threshold for measuring the new optimum,
            to only focus on significant changes. Default: 1e-4.
        threshold_mode (str): One of `rel`, `abs`. In `rel` mode,
            dynamic_threshold = best * ( 1 + threshold ) in 'max'
            mode or best * ( 1 - threshold ) in `min` mode.
            In `abs` mode, dynamic_threshold = best + threshold in
            `max` mode or best - threshold in `min` mode. Default: 'rel'.
        cooldown (int): Number of epochs to wait before resuming
            normal operation after lr has been reduced. Default: 0.
        min_lr (float or list): A scalar or a list of scalars. A
            lower bound on the learning rate of all param groups
            or each group respectively. Default: 0.
        eps (float): Minimal decay applied to lr. If the difference
            between new and old lr is smaller than eps, the update is
            ignored. Default: 1e-8.

    Example:
    -------
        >>> optimizer = AdamW(model.parameters(), lr=0.1, weight_decay=1e-3)
        >>> scheduler = ReduceLRWDOnPlateau(optimizer, 'min')
        >>> for epoch in range(10):
        >>>     train(...)
        >>>     val_loss = validate(...)
        >>>     # Note that step should be called after validate()
        >>>     scheduler.step(val_loss)
    """

    def step(self, metrics, epoch=None):
        current = metrics
        if epoch is None:
            epoch = self.last_epoch = self.last_epoch + 1
        self.last_epoch = epoch

        if self.is_better(current, self.best):
            self.best = current
            self.num_bad_epochs = 0
        else:
            self.num_bad_epochs += 1

        if self.in_cooldown:
            self.cooldown_counter -= 1
            self.num_bad_epochs = 0  # ignore any bad epochs in cooldown

        if self.num_bad_epochs > self.patience:
            self._reduce_lr(epoch)
            self._reduce_weight_decay(epoch)
            self.cooldown_counter = self.cooldown
            self.num_bad_epochs = 0

    def _reduce_weight_decay(self, epoch):
        for i, param_group in enumerate(self.optimizer.param_groups):
            if param_group["weight_decay"] != 0:
                old_weight_decay = float(param_group["weight_decay"])
                new_weight_decay = max(old_weight_decay * self.factor, self.min_lrs[i])
                if old_weight_decay - new_weight_decay > self.eps:
                    param_group["weight_decay"] = new_weight_decay
                    if self.verbose:
                        log.info(f"Epoch {epoch}: reducing weight decay factor of group {i} to {new_weight_decay:.4e}.")
