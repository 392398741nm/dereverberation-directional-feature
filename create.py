""" create directional spectrogram.

Usage:
```
    python create.py room_create
                     {TRAIN,SEEN,UNSEEN}
                     [--from_idx IDX] [-t TARGET] [--DF {IV, DirAC}] [--num_disk_workers N]
                     ...
```
More parameters are in `hparams.py`.
- IDX: feature index
- TARGET: name of the folder feature files will be saved. The folder is a child of `hp.path_feature`.
- DF: "IV" for using spatially-averaged intensity, "DirAC" for using direction vector.
- N: number of subprocesses to write files.
"""

import multiprocessing as mp
import os
from argparse import ArgumentParser, ArgumentError
from pathlib import Path
from typing import Tuple, TypeVar, Optional, List
from dataclasses import dataclass, asdict
from itertools import product
import cupy as cp
# noinspection PyUnresolvedReferences
import cupy.lib.stride_tricks

import librosa
import numpy as np
import scipy.io as scio
import scipy.signal as scsig
import soundfile as sf
from tqdm import tqdm

from hparams import hp

NDArray = TypeVar('NDArray', np.ndarray, cp.ndarray)


@dataclass
class SFTData:
    """ Constant Matrices/Vectors for Spherical Fourier Analysis

    """
    Yenc: NDArray = None  # Encoding Matrix
    bnkr_inv: NDArray = None  # the inverse of the modal strength

    # SIV
    Wpv_1_1_m1: Optional[NDArray] = None
    Wpv_1_0_0: Optional[NDArray] = None
    Wnv_1_0_0: Optional[NDArray] = None
    Wnv_1_1_1: Optional[NDArray] = None
    Vv_1_0_0: Optional[NDArray] = None
    Vv_1_1_0: Optional[NDArray] = None

    # DirAC
    T_real: Optional[NDArray] = None

    def get_for_intensity(self) -> Tuple:
        return (self.Wpv_1_1_m1, self.Wpv_1_0_0,
                self.Wnv_1_0_0, self.Wnv_1_1_1,
                self.Vv_1_0_0, self.Vv_1_1_0)

    def as_single_prec(self):
        """

        :rtype: SFTData
        """
        dict_single = dict()
        xp = cp.get_array_module(self.Yenc)
        for k, v in asdict(self).items():
            if v is None:
                continue

            if v.dtype == xp.float64:
                dict_single[k] = v.astype(xp.float32)
            elif v.dtype == xp.complex128:
                dict_single[k] = v.astype(xp.complex64)
            elif v.dtype == xp.float32:
                dict_single[k] = v
            elif v.dtype == xp.complex64:
                dict_single[k] = v
            else:
                raise NotImplementedError

        return SFTData(**dict_single)


def stft(data: NDArray, _win: NDArray):
    xp = cp.get_array_module(data)
    data = xp.pad(data,
                  ((0, 0), (hp.n_fft // 2, hp.n_fft // 2)),
                  mode='reflect')

    n_frame = (data.shape[1] - hp.l_frame) // hp.l_hop + 1

    spec = xp.lib.stride_tricks.as_strided(
        data,
        (data.shape[0], hp.l_frame, n_frame),
        (data.strides[0], data.strides[1], data.strides[1] * hp.l_hop)
    )
    spec = spec * _win[:, xp.newaxis]
    spec = xp.fft.fft(spec, axis=1)[:, :hp.n_freq, :]

    return spec


def filter_overlap_add(wave: NDArray, filter_fft: NDArray, _win: NDArray):
    # bnkr equalization in frequency domain
    xp = cp.get_array_module(wave)
    filter_fft = filter_fft[..., xp.newaxis]
    _win = _win[..., xp.newaxis]

    len_original = wave.shape[1]
    wave = xp.pad(wave,
                  ((0, 0), (hp.n_fft // 2, hp.n_fft // 2)),
                  mode='reflect')

    n_frame = len_original // hp.l_hop + 1
    len_istft = hp.n_fft + hp.l_hop * (n_frame - 1)

    strided = xp.lib.stride_tricks.as_strided(
        wave,
        (wave.shape[0], hp.l_frame, n_frame),
        (wave.strides[0], wave.strides[1], wave.strides[1] * hp.l_hop)
    )
    strided = strided * _win
    strided_filt = xp.fft.ifft(xp.fft.fft(strided, axis=1) * filter_fft, axis=1)
    strided_filt *= _win
    filtered = xp.zeros((wave.shape[0], len_istft), dtype=xp.complex64)
    startend = np.array([0, hp.l_frame])
    for i_frame in range(n_frame):
        filtered[:, slice(*startend)] += strided_filt[..., i_frame]
        startend += hp.l_hop

    # compensate artifact of stft/istft
    # noinspection PyTypeChecker
    artifact = librosa.filters.window_sumsquare(
        'hann',
        n_frame, win_length=hp.l_frame, n_fft=hp.n_fft, hop_length=hp.l_hop,
        dtype=np.float32
    )
    idxs_artifact = artifact > librosa.util.tiny(artifact)
    artifact = xp.array(artifact[idxs_artifact])

    filtered[:, idxs_artifact] /= artifact
    filtered = filtered[:, hp.n_fft // 2:]
    filtered = filtered[:, :len_original]

    return filtered if xp.iscomplexobj(wave) else filtered.real


# noinspection PyShadowingNames
def seltriag(Ain: NDArray, nrord: int, shft: Tuple[int, int]) -> NDArray:
    """ select spherical harmonics coefficients from Ain
     with $N$-`nrord` order, $m$+`shft[0]`, $n$+`shift[1]`

    :param Ain:
    :param nrord:
    :param shft:
    :return:
    """
    xp = cp.get_array_module(Ain)
    other_shape = Ain.shape[1:] if Ain.ndim > 1 else tuple()
    N = int(np.ceil(np.sqrt(Ain.shape[0])) - 1)
    idx = 0
    len_new = (N - nrord + 1)**2

    Aout = xp.zeros((len_new, *other_shape), dtype=Ain.dtype)
    for ii in range(N - nrord + 1):
        for jj in range(-ii, ii + 1):
            n, m = shft[0] + ii, shft[1] + jj
            idx_from = m + n * (n + 1)
            if -n <= m <= n and 0 <= n <= N and idx_from < Ain.shape[0]:
                Aout[idx] = Ain[idx_from]
            idx += 1
    return Aout


# noinspection PyShadowingNames
def calc_intensity(Asv: NDArray,
                   Wpv_1_1_m1: NDArray, Wpv_1_0_0: NDArray,
                   Wnv_1_0_0: NDArray, Wnv_1_1_1: NDArray,
                   Vv_1_0_0: NDArray, Vv_1_1_0: NDArray,
                   out: NDArray = None) -> NDArray:
    """ Asv(anm) (Order x ...) -> Intensity (... x 3)

    """

    xp = cp.get_array_module(Asv)
    other_shape = Asv.shape[1:] if Asv.ndim > 1 else tuple()

    aug1 = seltriag(Asv, 1, (0, 0)).conj()
    aug2 = (Wpv_1_1_m1 * seltriag(Asv, 1, (1, -1))
            - Wnv_1_0_0 * seltriag(Asv, 1, (-1, -1)))
    aug3 = (Wpv_1_0_0 * seltriag(Asv, 1, (-1, 1))
            - Wnv_1_1_1 * seltriag(Asv, 1, (1, 1)))
    aug4 = (Vv_1_0_0 * seltriag(Asv, 1, (-1, 0))
            + Vv_1_1_0 * seltriag(Asv, 1, (1, 0)))

    if out is None:
        out = xp.empty((*other_shape, 3), dtype=xp.float32)
    else:
        assert out.shape == (*other_shape, 3)
    (aug1 * (aug2 + aug3)).real.sum(axis=0, out=out[..., 0])
    out[..., 0] /= 2
    (aug1 * (aug2 - aug3) / 2j).real.sum(axis=0, out=out[..., 1])
    (aug1 * aug4).real.sum(axis=0, out=out[..., 2])
    out /= 2

    return out


def calc_mat_for_real_coeffs(N: int) -> np.ndarray:
    """ calculate matrix to convert complex SH coeffs to real

    :param N: n-order
    :return: (Order x Order)
    """
    matrix = np.zeros(((N + 1)**2, (N + 1)**2), dtype=np.complex64)
    matrix[0, 0] = 1
    if N > 0:
        idxs = (np.arange(N + 1) + 1)**2

        for n in range(1, N + 1):
            m1 = np.arange(n, dtype=np.float32)
            diag = np.concatenate((np.full(n, 1j, dtype=np.complex64), (0,), -(-1)**m1))

            m2 = m1[::-1]
            anti_diag = np.concatenate((1j * (-1)**m2, (0,), np.ones(n, dtype=np.complex64)))

            block = (np.diagflat(diag) + np.diagflat(anti_diag)[:, ::-1]) / np.sqrt(2)
            block[n, n] = 1.

            matrix[idxs[n - 1]:idxs[n], idxs[n - 1]:idxs[n]] = block

    return matrix.conj()


def calc_direction_vec(anm: NDArray, out: NDArray = None) -> NDArray:
    """ Calculate direciton vector in DirAC
     using Complex Spherical Harmonics Coefficients

    :param anm: (Order x ...)
    :param out: (... x 3)
    :return: (... x 3)
    """
    other_shape = anm.shape[1:] if anm.ndim > 1 else tuple()
    result = (anm[0].conj() * anm[[3, 1, 2]]).real
    result = result.transpose((*range(1, anm.ndim), 0))
    result *= np.sqrt(0.5)
    if out is None:
        return result
    else:
        assert out.shape == (*other_shape, 3)
        out[...] = result

    return out


def process():
    print_save_info(idx_start)
    range_feature = range(idx_start, n_feature)

    pool_propagater = mp.Pool(min(n_cuda_dev * 3, mp.cpu_count() // 2 - 1 - n_cuda_dev))
    pool_extractor = mp.Pool(n_cuda_dev)
    with mp.Manager() as manager:
        q_data = [manager.Queue(3) for _ in hp.device]
        q_out = manager.Queue(3 * n_cuda_dev)

        # open speech files
        speech = []
        for f_speech in flist_speech:
            speech.append(sf.read(str(f_speech))[0].astype(np.float32))

        # apply extractor first
        # extractor gets data from q_data, and sends the result to q_out
        pool_extractor.starmap_async(
            calc_dirspecs,
            [(dev,
              q_data[idx],
              len(list_feature[idx_start + idx::n_cuda_dev]),
              q_out)
             for idx, dev in enumerate(hp.device)]
        )
        pool_extractor.close()

        # apply propagater
        # propagater sends data to q_data
        for idx, (i_speech, _, i_loc) in zip(range_feature, list_feature[idx_start:]):
            pool_propagater.apply_async(
                propagate,
                (idx, i_speech, flist_speech[i_speech], speech[i_speech], i_loc,
                 q_data[(idx - idx_start) % n_cuda_dev])
            )
            # propagate(idx, i_speech, flist_speech[i_speech],
            #           speech[i_speech], i_loc,
            #           q_data[(idx - idx_start) % n_cuda_dev])
        pool_propagater.close()

        # save result feature
        pbar = tqdm(range(n_feature),
                    desc='create', dynamic_ncols=True, initial=idx_start)
        for _ in range_feature:
            idx, i_speech, i_loc, dict_result = q_out.get()
            p = path_result / hp.form_feature.format(idx, i_speech, hp.room_create, i_loc)
            np.savez(p, **dict_result)
            str_qsizes = ' '.join([f'{q.qsize()}' for q in q_data])
            pbar.set_postfix_str(f'[{str_qsizes}], {q_out.qsize()}')
            pbar.update()
        pbar.close()

        pool_propagater.join()
        pool_extractor.join()

    print_save_info(n_feature)


def propagate(idx: int, i_speech: int, f_speech: Path,
              data: np.ndarray, i_loc: int,
              queue: mp.Queue):
    # RIR Filtering
    data_room = scsig.fftconvolve(data[np.newaxis, :], RIRs[i_loc])

    # Propagation
    data = np.append(np.zeros(t_peak[i_loc], dtype=np.float32), data * amp_peak[i_loc])

    queue.put((idx, i_speech, f_speech, i_loc, data, data_room))


def calc_dirspecs(i_dev: int, q_data: mp.Queue, n_data: int, q_out: mp.Queue):
    """ create directional spectrogram.

    :param i_dev: GPU Device No.
    :param q_data:
    :param n_data:
    :param q_out:

    :return: None
    """

    # Ready CUDA
    cp.cuda.Device(i_dev).use()
    win_cp = cp.array(win)
    Ys_cp = cp.array(Ys)
    sftdata_cp = SFTData(
        **{k: cp.array(v) for k, v in asdict(sftdata).items() if v is not None}
    )

    for _ in range(n_data):
        idx, i_speech, f_speech, i_loc, data, data_room = q_data.get()
        data_cp = cp.array(data)  # n,
        data_room_cp = cp.array(data_room)  # N_MIC, n

        # Free-field
        # Order, n
        anm_time_cp = cp.outer(Ys_cp[i_loc].conj(), data_cp)  # complex coefficients
        if use_dirac:  # real coefficients
            anm_time_cp = (sftdata_cp.T_real @ anm_time_cp).real

        anm_spec_cp = stft(anm_time_cp, win_cp)  # Order, F, T

        dirspec_free_cp = cp.empty((hp.n_freq, anm_spec_cp.shape[2], 4),
                                   dtype=cp.float32)
        if use_dirac:
            calc_direction_vec(anm_spec_cp, out=dirspec_free_cp[..., :3])
        else:
            calc_intensity(anm_spec_cp, *sftdata_cp.get_for_intensity(),
                           out=dirspec_free_cp[..., :3])
        cp.abs(anm_spec_cp[0], out=dirspec_free_cp[..., 3])  # mag
        phase_free = cp.angle(anm_spec_cp[0])  # F, T

        # Room
        pnm_time_cp = sftdata_cp.Yenc @ data_room_cp  # Order, n
        # Order, F, T
        if use_dirac:  # real coefficients
            # bnkr equalization in frequency domain
            anm_time_cp = filter_overlap_add(pnm_time_cp,
                                             sftdata_cp.bnkr_inv[..., 0],
                                             win_cp)
            anm_t_real_cp = (sftdata_cp.T_real @ anm_time_cp).real
            anm_spec_cp = stft(anm_t_real_cp, win_cp)
        else:  # complex coefficients
            pnm_spec_cp = stft(pnm_time_cp, win_cp)
            anm_spec_cp = pnm_spec_cp * sftdata_cp.bnkr_inv[:, :hp.n_freq]

        dirspec_room_cp = cp.empty((hp.n_freq, anm_spec_cp.shape[2], 4),
                                   dtype=cp.float32)
        if use_dirac:
            calc_direction_vec(anm_spec_cp, out=dirspec_room_cp[..., :3])
        else:
            calc_intensity(anm_spec_cp, *sftdata_cp.get_for_intensity(),
                           out=dirspec_room_cp[..., :3])
        cp.abs(anm_spec_cp[0], out=dirspec_room_cp[..., 3])  # mag
        phase_room = cp.angle(anm_spec_cp[0])  # F, T

        # Save (F, T, C)
        dict_result = dict(path_speech=str(f_speech),
                           dirspec_free=cp.asnumpy(dirspec_free_cp),
                           dirspec_room=cp.asnumpy(dirspec_room_cp),
                           phase_free=cp.asnumpy(phase_free)[..., np.newaxis],
                           phase_room=cp.asnumpy(phase_room)[..., np.newaxis],
                           )
        q_out.put((idx, i_speech, i_loc, dict_result))


def print_save_info(i_feature: int):
    """ Print and save metadata.

    """
    print(f'{hp.DF}, {hp.room_create}, {args.kind_data}\n'
          f'Number of mic/source position pairs: {n_loc}\n'
          f'target folder: {path_result}\n'
          f'Feature files saved/total: {i_feature}/{len(list_feature)}\n')

    metadata = dict(fs=hp.fs,
                    n_fft=hp.n_fft,
                    n_freq=hp.n_freq,
                    l_frame=hp.l_frame,
                    l_hop=hp.l_hop,
                    n_loc=(n_loc,),
                    path_all_speech=[str(p) for p in flist_speech],
                    list_fname=list_feature_to_fname(list_feature),
                    rooms=(hp.room_create,),
                    )

    scio.savemat(f_metadata, metadata)


def list_feature_to_fname(list_feature: List[Tuple]) -> List[str]:
    return [
        hp.form_feature.format(i, *tup) for i, tup in enumerate(list_feature)
    ]


def list_fname_to_feature(list_fname: List[str]) -> List[Tuple]:
    list_feature = []
    for f in list_fname:
        f = f.rstrip().rstrip('.npz')
        _, i_speech, _, i_loc = f.split('_')
        if int(i_loc) < n_loc:
            list_feature.append((int(i_speech), hp.room_create, int(i_loc)))
    return list_feature


if __name__ == '__main__':
    # determined by sys argv
    parser = ArgumentParser()
    parser.add_argument('room_create')
    parser.add_argument('kind_data',
                        choices=('TRAIN', 'train',
                                 'SEEN', 'seen',
                                 'UNSEEN', 'unseen',
                                 ),
                        )
    parser.add_argument('-t', dest='target_folder', default='')
    parser.add_argument('--from', type=int, default=-1,
                        dest='from_idx')
    args = hp.parse_argument(parser, print_argument=False)
    use_dirac = hp.DF == 'DirAC'
    n_cuda_dev = len(hp.device)
    is_train = args.kind_data.lower() == 'train'

    # Paths
    path_speech = hp.dict_path['speech_train' if is_train else 'speech_test']

    if args.target_folder:
        path_result = hp.path_feature / args.target_folder
        if not is_train:
            path_result = path_result / 'TEST'
        path_result = path_result / args.kind_data.upper()
    else:
        path_result = hp.dict_path[f'feature_{args.kind_data.lower()}']
    os.makedirs(path_result, exist_ok=True)

    # RIR Data
    transfer_dict = scio.loadmat(str(hp.dict_path['RIR_Ys']), squeeze_me=True)
    kind_RIR = 'TEST' if args.kind_data.lower() == 'unseen' else 'TRAIN'
    RIRs = transfer_dict[f'RIR_{kind_RIR}'].transpose((2, 0, 1))
    n_loc, n_mic, len_RIR = RIRs.shape
    Ys = transfer_dict[f'Ys_{kind_RIR}'].T * np.sqrt(4 * np.pi)  # N_LOC x Order
    RIRs = RIRs.astype(np.float32)
    Ys = Ys.astype(np.complex64)

    # SFT Data
    sftdata = SFTData()
    sft_dict = scio.loadmat(
        str(hp.dict_path['sft_data']),
        variable_names=('bEQf', 'Yenc', 'Wnv', 'Wpv', 'Vv'),
        squeeze_me=True
    )
    sftdata.Yenc = sft_dict['Yenc'].T / np.sqrt(4 * np.pi) / n_mic  # Order x N_MIC
    sftdata.bnkr_inv = sft_dict['bEQf'].T[..., np.newaxis]  # Order x N_freq x 1
    sftdata.bnkr_inv = np.concatenate(
        (sftdata.bnkr_inv, sftdata.bnkr_inv[:, -2:0:-1].conj()), axis=1
    )  # Order x N_fft x 1

    if use_dirac:
        Ys = Ys[:, :4]
        sftdata.Yenc = sftdata.Yenc[:4]
        sftdata.bnkr_inv = sftdata.bnkr_inv[:4]
        sftdata.T_real = calc_mat_for_real_coeffs(1)
    else:
        Wnv = sft_dict['Wnv'].astype(np.complex64)[:, np.newaxis, np.newaxis]
        Wpv = sft_dict['Wpv'].astype(np.complex64)[:, np.newaxis, np.newaxis]
        Vv = sft_dict['Vv'].astype(np.complex64)[:, np.newaxis, np.newaxis]
        sftdata.Wpv_1_1_m1 = seltriag(Wpv, 1, (1, -1))
        sftdata.Wpv_1_0_0 = seltriag(Wpv, 1, (0, 0))
        sftdata.Wnv_1_0_0 = seltriag(Wnv, 1, (0, 0))
        sftdata.Wnv_1_1_1 = seltriag(Wnv, 1, (1, 1))
        sftdata.Vv_1_0_0 = seltriag(Vv, 1, (0, 0))
        sftdata.Vv_1_1_0 = seltriag(Vv, 1, (1, 0))

    sftdata = sftdata.as_single_prec()

    del sft_dict

    # propagation
    win = scsig.windows.hann(hp.l_frame, sym=False)
    win = win.astype(np.float32)
    p00_RIRs = np.einsum('ijk,j->ik', RIRs, sftdata.Yenc[0].real)  # n_loc x time
    a00_RIRs = filter_overlap_add(p00_RIRs, sftdata.bnkr_inv[0, :, 0], win)

    t_peak = a00_RIRs.argmax(axis=1)
    amp_peak = a00_RIRs.max(axis=1)

    f_metadata = path_result / 'metadata.mat'
    if hp.s_path_metadata:
        f_reference_meta = Path(hp.s_path_metadata)
        if not f_reference_meta.exists():
            raise Exception(f'"{hp.s_path_metadata}" doesn\'t exist.')
    elif f_metadata.exists():
        f_reference_meta = f_metadata
    else:
        f_reference_meta = None

    if f_reference_meta:
        metadata_ref = scio.loadmat(str(f_reference_meta),
                                    variable_names=('path_all_speech', 'list_fname'),
                                    chars_as_strings=True,
                                    squeeze_me=True)
        flist_speech = metadata_ref['path_all_speech']
        flist_speech = [Path(p.rstrip()) for p in flist_speech]
        n_speech = len(flist_speech)
        list_fname_refer = metadata_ref['list_fname']
        list_feature: List[Tuple] = list_fname_to_feature(list_fname_refer)
        n_feature = len(list_feature)
    else:
        flist_speech = list(path_speech.glob('**/*.WAV')) + list(path_speech.glob('**/*.wav'))
        n_speech = len(flist_speech)
        list_feature = [(i_speech, hp.room_create, i_loc)
                        for i_speech, i_loc in product(range(n_speech), range(n_loc))]

        if args.kind_data.lower() == 'train':
            n_feature = hp.n_data_per_room
        else:
            n_feature = hp.n_test_per_room
        idx_choice = np.random.choice(len(list_feature), n_feature, replace=False)
        idx_choice.sort()
        list_feature: List[Tuple] = [list_feature[i] for i in idx_choice]

    if n_feature < args.from_idx:
        raise ArgumentError

    # The index of the first speech file that have to be processed
    idx_exist = -2  # -2 means all files already exist
    for idx, tup in enumerate(list_feature):
        fname = hp.form_feature.format(idx, *tup)
        if not (path_result / fname).exists():
            idx_exist = idx - 1
            break

    if args.from_idx == -1:
        if idx_exist == -2:
            print_save_info(n_speech)
            exit(0)
        idx_start = idx_exist + 1
        should_ask_cont = False
    else:
        idx_start = args.from_idx
        should_ask_cont = True

    print(f'Start processing from the {idx_start}-th feature.')
    if should_ask_cont:
        while True:
            ans = input(f'{idx_exist} speech files were already processed. continue? (y/n)')
            if ans.lower() == 'y':
                break
            elif ans.lower() == 'n':
                exit(0)

    process()