import pandas as pd
import matplotlib.pyplot as plt
import matplotlib

import seaborn as sn
import numpy as np
from PIL import Image
import io

from sklearn.metrics import cohen_kappa_score
from sklearn.metrics import confusion_matrix
import pytorch_lightning as pl
from torch.utils import data as tdata
import torch.nn as nn
import albumentations
from modules import EfficientModel
from datasets import TileDataset
from utils import dict_to_args
import datetime

import torch
import random
import os
import argparse
from pathlib import Path
import pickle
from contribs.fancy_optimizers import Over9000, Ranger
from contribs.torch_utils import FlatCosineAnnealingLR
matplotlib.use('Agg')


def convert_to_image(cm):
    df_cm = pd.DataFrame(cm, index=[i for i in "012345"],
                         columns=[i for i in "012345"])
    plt.figure(figsize=(10, 7))
    sns_plot = sn.heatmap(df_cm, annot=True)
    buf = io.BytesIO()
    sns_plot.get_figure().savefig(buf)
    cm_image = np.array(Image.open(buf).resize((512, 512)))[:, :, :3]
    return cm_image


class LightModel(pl.LightningModule):

    def __init__(self, hparams, df_train, train_idx, val_idx, train_path):
        super().__init__()
        self.train_idx = train_idx
        self.val_idx = val_idx
        self.df_train = df_train
        self.train_path = train_path
        c_out = 5 if hparams.loss == 'bce' else 1
        self.model = EfficientModel(c_out=c_out,
                                    n_tiles=hparams.n_tiles,
                                    tile_size=hparams.tile_size,
                                    name=hparams.backbone,
                                    strategy=hparams.strategy,
                                    head=hparams.head
                                    )

        self.hparams = hparams
        self.trainset = None
        self.valset = None

    def forward(self, batch):
        return self.model(batch['image'])

    def prepare_data(self):

        transforms_train = albumentations.Compose([albumentations.Transpose(p=0.5),
                                                   albumentations.VerticalFlip(p=0.5),
                                                   albumentations.HorizontalFlip(p=0.5),
                                                   ])
        transforms_val = albumentations.Compose([])

        if self.hparams.strategy == 'bag':
            return_stitched = False
        else:
            return_stitched = True
        one_hot = True if self.hparams.loss == 'bce' else False

        self.trainsets = [TileDataset(self.train_path + '0/', self.df_train.iloc[self.train_idx], suffix='',
                                      one_hot=one_hot, return_stitched=return_stitched,
                                      num_tiles=self.hparams.n_tiles, transform=transforms_train)]

        # self.trainsets += [TileDataset(self.train_path + f'1/', self.df_train.iloc[self.train_idx], suffix=f'_{i}',
        #                                one_hot=one_hot, return_stitched=return_stitched,
        #                                num_tiles=self.hparams.n_tiles,
        #                                transform=transforms_train) for i in range(1, 4)]
        # self.trainsets += [TileDataset(self.train_path + f'2/', self.df_train.iloc[self.train_idx], suffix=f'_{i}',
        #                                one_hot=one_hot, return_stitched=return_stitched,
        #                                num_tiles=self.hparams.n_tiles,
        #                                transform=transforms_train) for i in range(4, 8)]
        # self.trainsets += [TileDataset(self.train_path + f'3/', self.df_train.iloc[self.train_idx], suffix=f'_{i}',
        #                                one_hot=one_hot, return_stitched=return_stitched,
        #                                num_tiles=self.hparams.n_tiles,
        #                                transform=transforms_train) for i in range(8, 12)]
        # self.trainsets += [TileDataset(self.train_path + f'4/', self.df_train.iloc[self.train_idx], suffix=f'_{i}',
        #                                one_hot=one_hot, return_stitched=return_stitched,
        #                                num_tiles=self.hparams.n_tiles,
        #                                transform=transforms_train) for i in range(12, 16)]

        # self.trainsets += [TileDataset(self.train_path + f'{i}/', self.df_train.iloc[self.train_idx], suffix=f'_{i}',
        #                                  one_hot=one_hot,
        #                                  num_tiles=self.hparams.n_tiles,
        #                                  transform=transforms_train) for i in range(1, 16)]

        self.valset = TileDataset(self.train_path + '0/', self.df_train.iloc[self.val_idx], suffix='',
                                  num_tiles=self.hparams.n_tiles, one_hot=one_hot, return_stitched=return_stitched,
                                  transform=transforms_val)

    def train_dataloader(self):
        rand_dataset = np.random.randint(0, len(self.trainsets))
        print('Using dataset', rand_dataset)
        train_dl = tdata.DataLoader(self.trainsets[rand_dataset], batch_size=self.hparams.batch_size, shuffle=True,
                                    num_workers=self.hparams.num_workers)
        return train_dl

    def val_dataloader(self):
        val_dl = tdata.DataLoader(self.valset, batch_size=self.hparams.batch_size, shuffle=False,
                                  num_workers=self.hparams.num_workers)
        return [val_dl]

    def cross_entropy_loss(self, logits, gt):
        if self.hparams.loss == 'bce':
            loss_fn = nn.BCEWithLogitsLoss()
        elif self.hparams.loss == 'mse':
            loss_fn = nn.MSELoss()
        return loss_fn(logits, gt)

    def configure_optimizers(self):

        if self.hparams.strategy == 'bag':
            params = [dict(params=self.model.feature_extractor.parameters(), lr=1e-4),
                      dict(params=self.model.head.parameters(), lr=1e-3)]
            optimizer = Over9000(params)
            schedulers = [FlatCosineAnnealingLR(optimizer, max_iter=self.hparams.epochs,
                                                step_size=self.hparams.step_size)]
        elif self.hparams.strategy == 'stitched':
            optimizer = torch.optim.Adam(self.model.parameters(), lr=self.hparams.init_lr)
            # Lightning will call scheduler only when doing optim, so after accumulation !
            total_steps = self.hparams.epochs * len(self.trainsets[0])//self.hparams.batch_size//self.hparams.accumulate
            schedulers = [{'scheduler': torch.optim.lr_scheduler.OneCycleLR(optimizer, max_lr=self.hparams.init_lr,
                                                                            div_factor=self.hparams.warmup_factor,
                                                                            total_steps=total_steps,
                                                                            pct_start=1 / self.hparams.epochs),
                           'interval': 'step',
                           'frequency': 1
                           }]

        self.optimizer = optimizer
        return [optimizer], schedulers

    def logits_to_preds(self, logits):
        if self.hparams.loss == 'bce':
            preds = logits.sigmoid().sum(1)
        elif self.hparams.loss == 'mse':
            preds = logits.squeeze(1)
        return preds

    def training_step(self, batch, batch_idx):
        logits = self(batch)
        loss = self.cross_entropy_loss(logits, batch['isup']).unsqueeze(0)
        preds = self.logits_to_preds(logits)
        return {'loss': loss, 'preds': preds, 'gt': batch['isup'], 'log': {'train_loss': loss}}

    def training_epoch_end(self, outputs):
        avg_loss = torch.cat([out['loss'] for out in outputs], dim=0).mean()
        tensorboard_logs = {'avg_train_loss': avg_loss}
        return {'avg_train_loss': avg_loss, 'log': tensorboard_logs}

    def validation_step(self, batch, batch_idx):
        logits = self(batch)
        loss = self.cross_entropy_loss(logits, batch['isup']).unsqueeze(0)
        preds = self.logits_to_preds(logits)

        return {'val_loss': loss, 'preds': preds, 'gt': batch['isup'], 'provider': batch['provider']}

    def validation_epoch_end(self, outputs):
        print(f'lr: {self.optimizer.param_groups[0]["lr"]:.7f}')
        avg_loss = torch.cat([out['val_loss'] for out in outputs], dim=0).mean()
        preds = torch.cat([out['preds'] for out in outputs], dim=0)
        gt = torch.cat([out['gt'] for out in outputs], dim=0)
        provider = np.concatenate([out['provider'] for out in outputs], axis=0)
        preds = preds.detach().cpu().numpy()
        gt = gt.detach().cpu().numpy()
        preds = np.round(preds)

        if self.hparams.loss == 'bce':
            gt = gt.sum(1)

        kappa = cohen_kappa_score(preds, gt, weights='quadratic')
        cm = confusion_matrix(gt, preds)
        print('CM')
        print(cm)
        cm_radboud = confusion_matrix(gt[provider == 'radboud'], preds[provider == 'radboud'])
        cm_karolinska = confusion_matrix(gt[provider == 'karolinska'], preds[provider == 'karolinska'])

        kappa_radboud = cohen_kappa_score(gt[provider == 'radboud'],
                                          preds[provider == 'radboud'],
                                          weights='quadratic')
        kappa_karolinska = cohen_kappa_score(gt[provider == 'karolinska'],
                                             preds[provider == 'karolinska'],
                                             weights='quadratic')
        cm_image = convert_to_image(cm)
        self.logger.experiment.add_image('CM', cm_image, self.global_step, dataformats='HWC')
        cm_image = convert_to_image(cm_radboud)
        self.logger.experiment.add_image('CM Radboud', cm_image, self.global_step, dataformats='HWC')
        cm_image = convert_to_image(cm_karolinska)
        self.logger.experiment.add_image('CM Karolinska', cm_image, self.global_step, dataformats='HWC')
        print(f'Epoch {self.current_epoch}: {avg_loss:.2f}, kappa: {kappa:.4f}')
        print('CM radboud')
        print(cm_radboud)
        print('kappa radboud:', kappa_radboud)
        print('CM karolinska')
        print(cm_karolinska)
        print('kappa karolinska:', kappa_karolinska)
        plt.close('all')
        kappa = torch.tensor(kappa)
        tensorboard_logs = {'val_loss': avg_loss, 'kappa': kappa, 'kappa_radboud': kappa_radboud,
                            'kappa_karolinska': kappa_karolinska}
        return {'avg_val_loss': avg_loss, 'log': tensorboard_logs}


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument("--root_dir", default='H:/', required=False)
    args = parser.parse_args()
    ROOT_PATH = args.root_dir
    SEED = 2020
    PRECISION = 16

    hparams = {'strategy': 'stitched',
               'backbone': 'efficientnet-b0',
               'head': 'basic',   # or attention
               'cancer_only': False,
               'predict_gleason': False,

               'loss': 'bce',
               'init_lr': 3e-4,
               'warmup_factor': 10,
               'step_size': 0.7,

               'n_tiles': 36,
               'level': 2,
               'scale': 1,
               'tile_size': 256,
               'num_workers': 8,
               'batch_size': 4,
               'accumulate': 2,
               'epochs': 30,
               }

    LEVEL = hparams['level']
    SIZE = hparams['tile_size']
    SCALE = hparams['scale']
    TRAIN_PATH = ROOT_PATH + f'/train_tiles_{SIZE}_{LEVEL}_{int(SCALE * 10)}/imgs/'
    CSV_PATH = './train.csv'  # This will include folds

    NAME = 'efficient0'
    OUTPUT_DIR = './lightning_logs'

    random.seed(SEED)
    os.environ['PYTHONHASHSEED'] = str(SEED)
    np.random.seed(SEED)
    torch.manual_seed(SEED)
    torch.cuda.manual_seed(SEED)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = True

    df_train = pd.read_csv(CSV_PATH)
    df_train['gleason_score'] = np.where(df_train['gleason_score'] == 'negative', '0+0', df_train['gleason_score'])
    fold_n = df_train['fold'].max()
    splits = []
    for i in range(0, fold_n + 1):
        train_idx = np.where(df_train['fold'] != i)[0]
        val_idx = np.where(df_train['fold'] == i)[0]
        splits.append((train_idx, val_idx))

    date = datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
    for fold, (train_idx, val_idx) in enumerate(splits):
        print(f'Fold {fold + 1}')
        tb_logger = pl.loggers.TensorBoardLogger(save_dir=OUTPUT_DIR,
                                                 name=f'{NAME}' + '-' + date,
                                                 version=f'fold_{fold + 1}')

        checkpoint_callback = pl.callbacks.ModelCheckpoint(filepath=tb_logger.log_dir + "/{epoch:02d}-{kappa:.4f}",
                                                           monitor='kappa', mode='max')

        model = LightModel(dict_to_args(hparams), df_train, train_idx, val_idx, TRAIN_PATH)
        trainer = pl.Trainer(gpus=[0], max_nb_epochs=hparams['epochs'], auto_lr_find=False,
                             gradient_clip_val=1,
                             logger=tb_logger,
                             accumulate_grad_batches=hparams['accumulate'],              # BatchNorm ?
                             checkpoint_callback=checkpoint_callback,
                             nb_sanity_val_steps=0,
                             precision=PRECISION,
                             reload_dataloaders_every_epoch=True
                             )
        trainer.fit(model)

        # Fold predictions
        print('Load best checkpoint')
        ckpt = list(Path(tb_logger.log_dir).glob('*.ckpt'))[0]
        ckpt = torch.load(ckpt)
        model.load_state_dict(ckpt['state_dict'])

        torch_model = model.model.eval().to('cuda')
        preds = []
        gt = []
        with torch.no_grad():
            for batch in model.val_dataloader()[0]:
                image = batch['image'].to('cuda')
                pred = torch_model(image)
                pred = torch.sigmoid(pred).sum(1)
                gt.append(batch['isup'].sum(1))
                preds.append(pred)
        preds = torch.cat(preds, dim=0).detach().cpu().numpy()
        gt = torch.cat(gt, dim=0).detach().cpu().numpy()
        pd.DataFrame({'val_idx': val_idx, 'preds': preds, 'gt': gt}).to_csv(
                     f'{OUTPUT_DIR}/{NAME}-{date}/fold{fold + 1}_preds.csv', index=False)
        with open(f'{OUTPUT_DIR}/{NAME}-{date}/hparams.pkl', 'wb') as file:
            pickle.dump(hparams, file)


        # Todo: One fold training
        break
