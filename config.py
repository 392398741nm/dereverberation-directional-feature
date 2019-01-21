import os
from typing import NamedTuple, Tuple

import deepdish as dd
import numpy as np
import scipy.io as scio
import torch
from torch import nn

from mypath import *

# CUDA
CUDA_DEVICES = list(range(torch.cuda.device_count()))
OUT_CUDA_DEV = 1

# Files
F_HPARAMS = 'hparams.h5'


class HyperParameters(NamedTuple):
    # n_per_frame: int

    CHANNELS = dict(x='all', y='alpha',
                    fname_wav=False,
                    )
    CH_BASE = 32

    n_epochs = 310
    batch_size = len(CUDA_DEVICES) * 5
    learning_rate = 5e-4
    n_file = 20 * 500

    # p = 0.5  # Dropout p

    # lr scheduler
    StepLR = dict(step_size=5, gamma=0.8)

    CosineAnnealingLR = dict(T_max=10,
                             eta_min=0,
                             )

    CosineLRWithRestarts = dict(restart_period=10,
                                t_mult=2,
                                eta_threshold=1.5,
                                )

    weight_decay = 1e-5  # Adam weight_decay

    weight_loss = (1, 0.7, 0.5)

    # def for_MLP(self) -> Tuple:
    #     n_input = self.L_cut_x * self.n_per_frame
    #     n_hidden = 17 * self.n_per_frame
    #     n_output = self.n_per_frame
    #     return (n_input, n_hidden, n_output, self.p)

    def get_for_UNet(self) -> Tuple:
        ch_in = 4 if self.CHANNELS['x'] == 'all' else 1
        ch_out = 4 if self.CHANNELS['y'] == 'all' else 1
        return ch_in, ch_out, self.CH_BASE


hp = HyperParameters()
CH_WITH_PHASE = dict(**hp.CHANNELS, x_phase=True, y_phase=True)
PERIOD_SAVE_STATE = hp.CosineLRWithRestarts['restart_period'] // 2

criterion = nn.MSELoss(reduction='sum')

# ========================for audio util==================================
N_GRIFFIN_LIM = 20

# metadata
_f_metadata = os.path.join(DICT_PATH['iv_train'], 'metadata.h5')
if os.path.isfile(_f_metadata):
    metadata = dd.io.load(_f_metadata)
    # all_files = metadata['path_wavfiles']
    L_hop = int(metadata['L_hop'])
    # N_freq = int(metadata['N_freq'])
    _N_LOC_TRAIN = int(metadata['N_LOC'])
    Fs = int(metadata['Fs'])
    N_fft = L_hop * 2

    # STFT/iSTFT arguments
    kwargs = dict(hop_length=L_hop, window='hann', center=True)
    KWARGS_STFT = dict(**kwargs, n_fft=N_fft, dtype=np.complex128)
    KWARGS_ISTFT = dict(**kwargs, dtype=np.float64)
    del kwargs

_f_metadata = os.path.join(DICT_PATH['iv_seen'], 'metadata.h5')
if os.path.isfile(_f_metadata):
    metadata = dd.io.load(_f_metadata)
    _N_LOC_SEEN = int(metadata['N_LOC'])

_f_metadata = os.path.join(DICT_PATH['iv_unseen'], 'metadata.h5')
if os.path.isfile(_f_metadata):
    metadata = dd.io.load(_f_metadata)
    _N_LOC_UNSEEN = int(metadata['N_LOC'])

    del metadata

try:
    # noinspection PyUnboundLocalVariable
    N_LOC = dict(train=_N_LOC_TRAIN, seen=_N_LOC_SEEN, unseen=_N_LOC_UNSEEN)
except NameError:
    print('Cannot get N_LOC')

# bEQspec
sft_dict = scio.loadmat(DICT_PATH['sft_data'],
                        variable_names=('bEQf',),
                        squeeze_me=True)
bEQf0 = sft_dict['bEQf'][:, 0][:, np.newaxis, np.newaxis]  # F, T, C
bEQf0_mag = np.abs(bEQf0)
bEQf0_angle = np.angle(bEQf0)
# bEQspec0 = torch.tensor(bEQspec0, dtype=torch.float32, device=OUT_DEVICE)
del sft_dict


# ========================for IVDataset==================================
IV_DATA_NAME = dict(x='/IV_room', y='/IV_free',
                    x_phase='/phase_room', y_phase='/phase_free',
                    fname_wav='/fname_wav',
                    out='/IV_estimated',
                    )
CH_SLICES = {'all': dd.aslice[:, :, :],
             'alpha': dd.aslice[:, :, -1:],
             True: None,
             }


def is_ivfile(f: os.DirEntry) -> bool:
    return (f.name.endswith('.h5')
            and not f.name.startswith('metadata')
            and not f.name.startswith('normconst_'))


# ====================== for Normalization Classes ============================
EPS_FOR_LOG = 1e-10

NORM_CLASS = 'LogMeanStdNormalization'

NORM_USING_ONLY_X = False
