import os
from os.path import join as pathjoin
from typing import Dict, Sequence, Tuple

import torch
from numpy import ndarray
from torch import nn, Tensor
from torch.utils.data import DataLoader
from torchsummary import summary
from tqdm import tqdm

import config as cfg
from adamwr import AdamW, CosineLRWithRestarts
from audio_utils import delta
from dirspecgram import DirSpecDataset
from models import UNet
from stft import STFT
from tbXwriter import CustomWriter
from utils import (MultipleOptimizer,
                   MultipleScheduler,
                   arr2str,
                   print_to_file,
                   )


class TrainerMeta(type):
    def __call__(cls, *args, **kwargs):
        if cls is Trainer:
            if cfg.hp.method == 'mag':
                return MagTrainer(*args, **kwargs)
            elif cfg.hp.method == 'complex':
                return ComplexTrainer(*args, **kwargs)
        else:
            return type.__call__(cls, *args, **kwargs)


class Trainer(metaclass=TrainerMeta):
    __slots__ = ('model', 'model_name',
                 'use_cuda', 'x_device', 'y_device',
                 'criterion', 'name_loss_terms',
                 'writer',
                 )

    def __new__(cls, *args, **kwargs):
        if cfg.hp.method == 'mag':
            return super().__new__(MagTrainer)
        elif cfg.hp.method == 'complex':
            return super().__new__(ComplexTrainer)

    def __init__(self, model_name: str, use_cuda=True):
        self.model_name = model_name
        self.use_cuda = use_cuda
        if model_name == 'UNet':
            module = UNet
        else:
            raise NotImplementedError
        self.model = nn.DataParallel(module(*cfg.hp.get_for_UNet()),
                                     device_ids=cfg.CUDA_DEVICES,
                                     output_device=cfg.OUT_CUDA_DEV)
        if use_cuda:
            self.model = self.model.cuda(device=cfg.CUDA_DEVICES[0])
            self.x_device = torch.device(f'cuda:{cfg.CUDA_DEVICES[0]}')
            self.y_device = torch.device(f'cuda:{cfg.OUT_CUDA_DEV}')
        else:
            self.model = self.model.module.cpu()
            self.x_device = torch.device('cpu')
            self.y_device = torch.device('cpu')

        self.criterion = cfg.criterion
        self.name_loss_terms = ''

        self.writer: CustomWriter = None

    def _pre(self, data: Dict[str, ndarray], dataset: DirSpecDataset) \
            -> Tuple[Tensor, Tensor]:
        pass

    def _post_one(self, output: Tensor, Ts: ndarray,
                  idx: int, dataset: DirSpecDataset) -> Dict[str, ndarray]:
        pass

    def _calc_loss(self, y: Tensor, output: Tensor,
                   T_ys: Sequence[int], dataset) -> Tensor:
        pass

    def train(self, loader_train: DataLoader, loader_valid: DataLoader, dir_result: str,
              first_epoch=0, f_state_dict=''):
        f_prefix = pathjoin(dir_result, f'{self.model_name}_')

        # Optimizer
        param1 = [p for m in self.model.modules() if not isinstance(m, nn.PReLU)
                  for p in m.parameters()]
        param2 = [p for m in self.model.modules() if isinstance(m, nn.PReLU)
                  for p in m.parameters()]

        optimizer = MultipleOptimizer(
            # torch.optim.Adam(
            AdamW(
                param1,
                lr=cfg.hp.learning_rate,
                weight_decay=cfg.hp.weight_decay,
            ) if param1 else None,
            # torch.optim.Adam(
            AdamW(
                param2,
                lr=cfg.hp.learning_rate,
            ) if param2 else None,
        )

        # Load State Dict
        if f_state_dict:
            tup = torch.load(f_state_dict)
            try:
                self.model.module.load_state_dict(tup[0])
                optimizer.load_state_dict(tup[1])
            except:
                raise Exception('The model is different from the state dict.')

        # Learning Rates Scheduler
        scheduler = MultipleScheduler(CosineLRWithRestarts,
                                      optimizer,
                                      batch_size=cfg.hp.batch_size,
                                      epoch_size=len(loader_train.dataset),
                                      last_epoch=first_epoch - 1,
                                      **cfg.hp.CosineLRWithRestarts)

        avg_loss = torch.zeros(cfg.N_LOSS_TERM, device=self.y_device)

        self.writer = CustomWriter(dir_result, purge_step=first_epoch)
        print_to_file(
            pathjoin(dir_result, 'summary'),
            summary,
            (self.model.module, (cfg.hp.get_for_UNet()[0], cfg.N_freq, 256)),
        )
        # self.writer.add_graph(self.model.module.cpu(),
        #                       torch.zeros(1, cfg.hp.get_for_UNet()[0], 256, 256),
        #                       True,
        #                       )

        # Start Training
        for epoch in range(first_epoch, cfg.hp.n_epochs):

            print()
            scheduler.step()
            pbar = tqdm(loader_train, desc=f'epoch {epoch:3d}', postfix='[0]', dynamic_ncols=True)

            for i_iter, data in enumerate(pbar):
                # get data
                x, y = self._pre(data, loader_train.dataset)  # B, C, F, T
                T_ys = data['T_ys']

                # forward
                output = self.model(x)[..., :y.shape[-1]]  # B, C, F, T

                loss = self._calc_loss(y, output, T_ys, loader_train.dataset)
                loss_sum = loss.sum()

                # backward
                optimizer.zero_grad()
                loss_sum.backward()
                if epoch <= 2:
                    gradnorm = nn.utils.clip_grad_norm_(self.model.parameters(), 10**10)
                    self.writer.add_scalar('train/grad', gradnorm,
                                           epoch * len(loader_train) + i_iter)
                    del gradnorm

                optimizer.step()
                scheduler.batch_step()

                # print
                with torch.no_grad():
                    avg_loss += loss
                    loss = loss.cpu().numpy() / len(T_ys)
                    pbar.set_postfix_str(arr2str(loss, ndigits=1))

            avg_loss /= len(loader_train.dataset)
            tag = 'loss/train'
            self.writer.add_scalar(tag, avg_loss.sum().item(), epoch)
            if len(self.name_loss_terms) > 1:
                for idx, (n, ll) in enumerate(zip(self.name_loss_terms, avg_loss)):
                    self.writer.add_scalar(f'{tag}/{idx + 1}. {n}', ll.item(), epoch)

            # Validation
            self.validate(loader_valid, f_prefix, epoch)

            # save loss & model
            if epoch % cfg.PERIOD_SAVE_STATE == cfg.PERIOD_SAVE_STATE - 1:
                torch.save(
                    (self.model.module.state_dict(),
                     optimizer.state_dict(),
                     ),
                    f'{f_prefix}{epoch}.pt'
                )

    @torch.no_grad()
    def validate(self, loader: DataLoader, f_prefix: str, epoch: int):
        """ Evaluate the performance of the model.

        :param loader: DataLoader to use.
        :param f_prefix: path and prefix of the result files.
        :param epoch:
        """

        self.model.eval()

        avg_loss = torch.zeros(cfg.N_LOSS_TERM, device=self.y_device)

        pbar = tqdm(loader, desc=f'validate ', postfix='[0]', dynamic_ncols=True)
        for i_iter, data in enumerate(pbar):
            # get data
            x, y = self._pre(data, loader.dataset)  # B, C, F, T
            T_ys = data['T_ys']

            # forward
            output = self.model(x)[..., :y.shape[-1]]

            # loss
            loss = self._calc_loss(y, output, T_ys, loader.dataset)
            avg_loss += loss

            # print
            loss = loss.cpu().numpy() / len(T_ys)
            pbar.set_postfix_str(arr2str(loss, ndigits=1))

            # write summary
            if not i_iter:
                # F, T, C
                if not self.writer.one_sample:
                    self.writer.one_sample = DirSpecDataset.decollate_padded(data, 0)

                out_one = self._post_one(output, T_ys, 0, loader.dataset)

                DirSpecDataset.save_iv(f'{f_prefix}IV_{epoch}',
                                       **self.writer.one_sample, **out_one)

                # Process(
                #     target=write_one,
                #     args=(x_one, y_one, x_ph_one, y_ph_one, out_one, epoch)
                # ).start()
                self.writer.write_one(epoch, **out_one, group='train')

        avg_loss /= len(loader.dataset)
        tag = 'loss/valid'
        self.writer.add_scalar(tag, avg_loss.sum().item(), epoch)
        if len(self.name_loss_terms) > 1:
            for idx, (n, ll) in enumerate(zip(self.name_loss_terms, avg_loss)):
                self.writer.add_scalar(f'{tag}/{idx + 1}. {n}', ll.item(), epoch)

        self.model.train()

        return avg_loss

    @torch.no_grad()
    def test(self, loader: DataLoader, dir_result: str, f_state_dict: str):
        if self.use_cuda:
            self.model.module.load_state_dict(torch.load(f_state_dict)[0])
        else:
            self.model.load_state_dict(torch.load(f_state_dict)[0])
        self.writer = CustomWriter(dir_result)

        group = os.path.basename(dir_result)

        avg_measure = None
        self.model.eval()

        pbar = tqdm(loader, desc=group, dynamic_ncols=True)
        for i_iter, data in enumerate(pbar):
            # get data
            x, y = self._pre(data, loader.dataset)  # B, C, F, T
            T_ys = data['T_ys']

            # forward
            output = self.model(x)[..., :y.shape[-1]]

            # write summary
            # F, T, C
            one_sample = DirSpecDataset.decollate_padded(data, 0)

            out_one = self._post_one(output, T_ys, 0, loader.dataset)

            DirSpecDataset.save_iv(pathjoin(dir_result, f'IV_{i_iter}'),
                                   **one_sample, **out_one)

            measure = self.writer.write_one(i_iter, **out_one, group=group, **one_sample)
            if avg_measure is None:
                avg_measure = measure
            else:
                avg_measure += measure

            # print
            str_measure = arr2str(measure).replace('\n', '; ')
            pbar.write(str_measure)

        self.model.train()

        avg_measure /= len(loader.dataset)

        self.writer.add_text(f'{group}/Average Measure/Proposed', str(avg_measure[0]))
        self.writer.add_text(f'{group}/Average Measure/Reverberant', str(avg_measure[1]))
        self.writer.close()  # Explicitly close

        print()
        str_avg_measure = arr2str(avg_measure).replace('\n', '; ')
        print(f'Average: {str_avg_measure}')


class MagTrainer(Trainer):
    def _pre(self, data: Dict[str, ndarray], dataset: DirSpecDataset) \
            -> Tuple[Tensor, Tensor]:
        # B, F, T, C
        x = data['x']
        y = data['y']

        x = x.to(self.x_device)
        y = y.to(self.y_device)

        x = dataset.preprocess_('x', x)
        y = dataset.preprocess_('y', y)

        if self.model_name == 'UNet':
            # B, C, F, T
            x = x.permute(0, -1, -3, -2)
            y = y.permute(0, -1, -3, -2)

        return x, y

    @torch.no_grad()
    def _post_one(self, output: Tensor, Ts: ndarray,
                  idx: int, dataset: DirSpecDataset) -> Dict[str, ndarray]:
        if self.model_name == 'UNet':
            one = output[idx, :, :, :Ts[idx]].permute(1, 2, 0)  # F, T, C
        else:
            # one = output[idx, :, :, :Ts[idx]]  # C, F, T
            raise NotImplementedError

        one = dataset.postprocess_('y', one)
        one = one.cpu().numpy()

        return dict(out=one)

    def _calc_loss(self, y: Tensor, output: Tensor,
                   T_ys: Sequence[int], dataset) -> Tensor:
        if not self.name_loss_terms:
            self.name_loss_terms = ('',)
        y = [y, None, None]
        output = [output, None, None]
        for i_dyn in range(1, 3):
            if cfg.hp.weight_loss[i_dyn] > 0:
                y[i_dyn], output[i_dyn] \
                    = delta(y[i_dyn - 1], output[i_dyn - 1], axis=-1)

        # Loss
        loss = torch.zeros(1, device=self.y_device)
        for i_dyn, (y_dyn, out_dyn) in enumerate(zip(y, output)):
            if cfg.hp.weight_loss[i_dyn] > 0:
                for T, item_y, item_out in zip(T_ys, y_dyn, out_dyn):
                    loss += (cfg.hp.weight_loss[i_dyn] / int(T)
                             * self.criterion(item_out[:, :, :T - 4 * i_dyn],
                                              item_y[:, :, :T - 4 * i_dyn]))
        return loss


class ComplexTrainer(Trainer):
    __slots__ = ('stft_module',
                 )

    def __init__(self, model_name: str, use_cuda=True):
        super().__init__(model_name, use_cuda)
        self.stft_module = STFT(cfg.N_fft, cfg.L_hop).cuda(self.y_device)

    def _pre(self, data: Dict[str, ndarray], dataset: DirSpecDataset) \
            -> Tuple[Tensor, Tensor]:
        # B, F, T, C
        x = data['x']
        y = data['y']
        x_phase = data['x_phase']
        y_phase = data['y_phase']

        x = x.to(self.x_device)
        y = y.to(self.y_device)
        x_phase = x_phase.to(self.x_device)
        y_phase = y_phase.to(self.y_device)

        x = dataset.preprocess_('x', x, x_phase)
        y = dataset.preprocess_('y', y, y_phase)

        if self.model_name == 'UNet':
            # B, C, F, T
            x = x.permute(0, -1, -3, -2)
            y = y.permute(0, -1, -3, -2)

        return x, y

    @torch.no_grad()
    def _post_one(self, output: Tensor, Ts: ndarray,
                  idx: int, dataset: DirSpecDataset) -> Dict[str, ndarray]:
        if self.model_name == 'UNet':
            one = output[idx, :, :, :Ts[idx]].permute(1, 2, 0)  # F, T, C
        else:
            # one = output[idx, :, :, :Ts[idx]]  # C, F, T
            raise NotImplementedError

        one, one_phase = dataset.postprocess_('y', one)
        one = one.cpu().numpy()
        one_phase = one_phase.cpu().numpy()

        return dict(out=one, out_phase=one_phase)

    def _calc_loss(self, y: Tensor, output: Tensor,
                   T_ys: Sequence[int], dataset) -> Tensor:
        if not self.name_loss_terms:
            self.name_loss_terms = ('mse', 'mse of log mag', 'mse of wave')
        loss = torch.zeros(3, device=self.y_device)
        for T, item_y, item_out in zip(T_ys, y, output):
            if cfg.hp.weight_loss[0] > 0:
                loss[0] += self.criterion(item_out[:, :, :T], item_y[:, :, :T]) / int(T)

            if not (cfg.hp.weight_loss[1] or cfg.hp.weight_loss[2]):
                continue
            # F, T, C
            mag_out, phase_out \
                = dataset.postprocess('y', item_out[:, :, :T].permute(1, 2, 0))
            mag_y, phase_y \
                = dataset.postprocess('y', item_y[:, :, :T].permute(1, 2, 0))

            if cfg.hp.weight_loss[1] > 0:
                # F, T, C
                mag_norm_out = dataset.preprocess('y', mag_out, idx=1)
                mag_norm_y = dataset.preprocess('y', mag_y, idx=1)

                loss[1] += self.criterion(mag_norm_out, mag_norm_y) / int(T)
            if cfg.hp.weight_loss[2] > 0:
                # 1, n
                wav_out = self.stft_module.inverse(mag_out.permute(2, 0, 1),
                                                   phase_out.permute(2, 0, 1))
                wav_y = self.stft_module.inverse(mag_y.permute(2, 0, 1),
                                                 phase_y.permute(2, 0, 1))

                temp = self.criterion(wav_out, wav_y) / int(T)
                if not torch.isnan(temp) and temp < 10**4:
                    loss[2] += temp

        for w, ll in zip(cfg.hp.weight_loss, loss):
            ll *= w

        return loss
