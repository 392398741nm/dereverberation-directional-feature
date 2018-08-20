import pdb  # noqa: F401

import torch
import scipy.io as scio
import sys

fname = sys.argv[1]
if not fname.endswith('.pt'):
    fname += '.pt'

state_dict = torch.load(fname, map_location=torch.device('cpu'))

dict_np = {key.replace('.', '_'): value.numpy()
           for key, value in state_dict.items()}

length = max([len(k) for k in dict_np.keys()])
for key, value in dict_np:
    print(f'{key:<{length}}: ndarray of shape {value.shape}')

scio.savemat(fname.replace('.pt', '.mat'), dict_np)