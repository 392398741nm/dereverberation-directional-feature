import numpy as np

import torch

import os
import gc


def static_vars(**kwargs):
    def decorate(func):
        for k, a in kwargs.items():
            setattr(func, k, a)
        return func
    return decorate


class MultipleOptimizer(object):
    def __init__(self, *op):
        self._optimizers = [item for item in op if item]

    def zero_grad(self):
        for op in self._optimizers:
            op.zero_grad()

    def step(self):
        for op in self._optimizers:
            op.step()

    def state_dict(self):
        return [op.state_dict() for op in self._optimizers]

    def load_state_dict(self, state_dicts):
        return [op.load_state_dict(st) for op, st in zip(self._optimizers, state_dicts)]

    def __len__(self):
        return len(self._optimizers)

    def __getitem__(self, idx: int):
        return self._optimizers[idx]


class MultipleScheduler(object):
    def __init__(self, cls_scheduler: type,
                 optimizers: MultipleOptimizer, *args, **kwargs):
        self._schedulers = []
        for op in optimizers:
            self._schedulers.append(cls_scheduler(op, *args, **kwargs))

    def step(self):
        for sch in self._schedulers:
            sch.step()

    def batch_step(self):
        for sch in self._schedulers:
            sch.batch_step()

    def __len__(self):
        return len(self._schedulers)

    def __getitem__(self, idx: int):
        return self._schedulers[idx]


def arr2str(a, format_='e', n_decimal=2):
    return np.array2string(
        a,
        formatter={'float_kind': lambda x: f'{x:.{n_decimal}{format_}}'}
    )


def print_progress(iteration: int, total: int, prefix='', suffix='',
                   decimals=1, len_bar=0):
    """
    Print Progress Bar
    """
    percent = f'{100 * iteration / total:>{decimals+4}.{decimals}f}'
    if len_bar == 0:
        len_bar = (min(os.get_terminal_size().columns, 80)
                   - len(prefix) - len(percent) - len(suffix) - 11)

    len_filled = len_bar * iteration // total
    bar = '#' * len_filled + '-' * (len_bar - len_filled)

    print(f'{prefix} |{bar}| {percent}% {suffix}', end='\r')
    if iteration == total:
        print('')


def print_cuda_tensors():
    """
    Print All CUDA Tensors
    """
    for obj in gc.get_objects():
        try:
            if torch.is_tensor(obj) \
                    or (hasattr(obj, 'data') and torch.is_tensor(obj.data)):
                print(type(obj), obj.size(), obj.device)
        finally:
            pass
