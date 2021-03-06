import pickle
import torch
import numpy as np
import matplotlib.pyplot as plt
import pytorch_lightning as pl

import wandb
from argparse import ArgumentParser
from torch.utils.data import DataLoader
from pytorch_lightning.callbacks import ModelCheckpoint

from model import SpatiotemporalLightningModule
from util import get_device, NumpyDataset


if __name__ == "__main__":

    """
    This class has the main training loop and stores the best predictions and saves results.

    Parameters:
   
    use_evt - boolean, whether or not to incoporate EVT into mixture model. If false becomes Hurdle baseline
    variable_thresh - boolean, randomly chooses thresholds for each location and window each batch
    main_func - string, density function for non-zero non-execss values. Must be lognormal
    ymax - scalar, max value of y that must be assigned non-zero density by GPD
    
    mean_multiplier - scalar, weight to assign MSE loss term (the other loss term is NLK)
    dropout_multiplier - scalar, weight for dropout regularization in Vandal et al implementation
    quantile - scalar, what quantile to use to define the excess threshold. If variable_thresh is True then the threshold
               determined by quantile will only be used for evaluation purposes while the mixture model's threshold
               will be random.
    max_epochs - int, number of epochs to train
    model_params - dictionary, contains parameters for CNN
    lr - scalar, learning rate
    use_mc - boolean, whether or not to use monte carlo dropout. Setting to True is necessary for Vandal et al
    mc_forwards - int, number of monte carlo forward passes to use for Vandal et al
    continuous_evt - boolean, whether to force the mixture model to be continuous -- doesn't work well
    """

    parser = ArgumentParser()
    parser = pl.Trainer.add_argparse_args(parser)

    # add model setup options
    parser.add_argument("--model", default="ding", type=str, help="Model to train",
                        choices=[
                            "bernoulli-lognormal-gp-variable",  # proposed model with variable threshold
                            "bernoulli-lognormal-gp-fixed",     # proposed model with fixed threshold
                            "bernoulli-lognormal",              # ablation of proposed model with hurdle only
                            "deterministic-cnn",                # ablation of proposed model with deterministic preds
                            "vandal",                           # baseline from Vandal et al.
                            "ding",                             # baseline from Ding et al.
                            "kong",                             # baseline from Kong et al.
                        ])
    parser.add_argument("--n_train", default=731, type=tuple, help="Number of samples to use for training")
    parser.add_argument("--n_val", default=104, type=tuple, help="Number of samples to use for validation")
    # note that n_test is implicitly defined by n - n_train - n_val with n = 939
    parser.add_argument("--batch_size", default=104, type=int, help="Batch size to train with")
    parser.add_argument("--mean_multiplier", default=0.9, type=float, help="Weight assigned to RMSE loss term (1 - complement is weight assigned to NLL term (probabilistic models) or EVL term (Ding et al.)).")
    parser.add_argument("--dropout_multiplier", default=1e-2, type=float, help="Weight assigned to dropout loss term in Vandal baseline")
    parser.add_argument("--quantile", default=0.6, type=float, help="Quantile used to define excess threshold in proposed model; used only for evaluation if variable threshold model")
    parser.add_argument("--continuous_evt", default=False, type=eval, help="Whether to constrain mixture to be continuous; appealing in theory but performs poorly in practice")
    parser.add_argument("--ev_index", default=1.0, type=float, help="Extreme value index hyperparameter for Ding et al")
    parser.add_argument("--mc_forwards", default=8, type=float, help="Number of Monte Carlo passes to use in Vandal et al")

    # add training setup options
    parser.add_argument("--wandb_name", default="default", type=str, help="Name of wandb run")
    parser.add_argument("--n_epoch", default=200, type=int, help="Number of epochs")
    parser.add_argument("--seed", default=1, type=int, help="Random seed")
    parser.add_argument("--lr", default=1e-4, type=float, help="Learning rate")
    args = parser.parse_args()
    args.max_epochs = args.n_epoch
    print(f"Starting run with args: {args}")

    # configure data with log-transform, standardization on x, and random shuffle
    with open("../data/subx/all_data.pickle", "rb") as f:
        data = pickle.load(f)
    x, y, rand_inds = data["x"], data["y"], data["rand_inds"]
    x = x[rand_inds[:, args.seed]]
    y = y[rand_inds[:, args.seed]]
    x = np.log(x + 1)
    mu_x, sigma_x = np.nanmean(x[:args.n_train + args.n_val]), np.nanstd(x[:args.n_train + args.n_val])
    x = (x - mu_x) / sigma_x
    train_dataset = NumpyDataset(x[:args.n_train], y[:args.n_train])
    val_dataset = NumpyDataset(x[args.n_train:args.n_train + args.n_val], y[args.n_train:args.n_train + args.n_val])
    test_dataset = NumpyDataset(x[args.n_train + args.n_val:], y[args.n_train + args.n_val:])
    train_dataloader = DataLoader(train_dataset, batch_size=args.batch_size, num_workers=1, shuffle=True)
    val_dataloader = DataLoader(val_dataset, batch_size=args.batch_size, num_workers=1)
    test_dataloader = DataLoader(test_dataset, batch_size=args.batch_size, num_workers=1)

    # configure parameters of model backbone (3D CNN or GRU) and spatiotemporal model
    model_params = {
        "ndim": 11,
        "hdim": 10,
        "odim": 6,
        "ksize": (3, 3, 3),
        "padding": (0, 1, 1),
        "bn": False,
        "act": "relu",
        "variable_thresh": False,
        "use_mc": False
    }
    st_params = {
        "use_evt": False,
        "moderate_func": "lognormal",
        "ymax": 250,
        "mean_multiplier": args.mean_multiplier,
        "dropout_multiplier": args.dropout_multiplier,
        "continuous_evt": args.continuous_evt,
        "quantile": args.quantile,
        "variable_thresh": False,
        "use_mc": False,
        "mc_forwards": 0,
        "backbone": "cnn",
        "deterministic": False,
        "ev_index": args.ev_index
    }

    # tweak parameters depending on model choice
    if args.model == "bernoulli-lognormal-gp-variable":
        model_params["variable_thresh"] = st_params["variable_thresh"] = True
        st_params["use_evt"] = True
    elif args.model == "bernoulli-lognormal-gp-fixed":
        st_params["use_evt"] = True
    elif args.model == "bernoulli-lognormal":
        pass    # no modifications here
    elif args.model == "deterministic-cnn":
        model_params["odim"] = 1
        st_params["deterministic"] = True
    elif args.model == "vandal":
        model_params["use_mc"] = st_params["use_mc"] = True
        st_params["mc_forwards"] = args.mc_forwards
    elif args.model == "ding":
        st_params["backbone"] = "ding"
        st_params["deterministic"] = True
        st_params["use_evt"] = True
        model_params = {
            "forecast_horizon": 1,
            "ndim": 11,
            "hdim": 10,
            "odim": 2,    # y_hat and p_extreme in channel dimension
            "spatial_dims": (29, 59),
            "window_size": 7,
            "memory_dim": 7,
            "context_size": 7
        }
    elif args.model == "kong":
        raise NotImplementedError()
    else:
        raise ValueError()

    # configure lightning module wrapper
    lightning_module = SpatiotemporalLightningModule(st_params=st_params, model_params=model_params,
                                                     seed=args.seed, lr=args.lr, n_epoch=args.max_epochs)

    # wandb logging
    wandb.init(project="demm", group=args.model)
    if args.wandb_name != "default":
        wandb.run.name = args.wandb_name  # continue logging on previous run
    wandb_logger = pl.loggers.WandbLogger(project="demm", group=args.model)
    wandb_logger.watch(lightning_module, log="all", log_freq=50)
    wandb_logger.experiment.config.update(args)

    # trainer configuration
    trainer = pl.Trainer.from_argparse_args(args)
    trainer = pl.Trainer.from_argparse_args(args)
    trainer.logger = wandb_logger
    checkpoint_callback = ModelCheckpoint(monitor="v_loss")
    trainer.callbacks.append(checkpoint_callback)

    # train and save best models
    print(f"Starting training.")
    trainer.fit(lightning_module, train_dataloader, val_dataloader)
    wandb_logger.experiment.config.update({"best_model_path": checkpoint_callback.best_model_path,
                                           "last_model_path": checkpoint_callback.last_model_path})
    print(f"Done training.")
    print(f"Best Model: {checkpoint_callback.best_model_path}")
    print(f"Last Model: {checkpoint_callback.last_model_path}")

    # test with best validation loss model
    print(f"Starting testing with {checkpoint_callback.best_model_path}.")
    lightning_module = SpatiotemporalLightningModule.load_from_checkpoint(checkpoint_callback.best_model_path)
    lightning_module.to(device=get_device(), dtype=torch.float)
    trainer.test(lightning_module, test_dataloader)
    print(f"Done testing.")
