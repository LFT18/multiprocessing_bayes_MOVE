__all__ = ["identify_associations"]

from functools import reduce
from pathlib import Path
from typing import Literal, cast

import hydra
import numpy as np
import pandas as pd
from omegaconf import OmegaConf
from torch.utils.data import SequentialSampler

from move.conf.schema import (
    IdentifyAssociationsBayesConfig,
    IdentifyAssociationsConfig,
    IdentifyAssociationsTTestConfig,
    MOVEConfig,
)
from move.core.logging import get_logger
from move.core.typing import FloatArray
from move.data import io
from move.data.dataloaders import MOVEDataset, make_dataloader
from move.data.perturbations import perturb_data
from move.data.preprocessing import one_hot_encode_single
from move.models.vae import VAE


def _get_type_task(
    task_config: IdentifyAssociationsConfig,
) -> Literal["bayes", "ttest"]:
    task_type = OmegaConf.get_type(task_config)
    if task_type is IdentifyAssociationsBayesConfig:
        return "bayes"
    if task_type is IdentifyAssociationsTTestConfig:
        return "ttest"
    raise ValueError("Unsupported type of task!")


def identify_associations(config: MOVEConfig):
    """Trains multiple models to identify associations between the dataset
    of interest and the continuous datasets."""

    logger = get_logger(__name__)
    task_config = cast(IdentifyAssociationsConfig, config.task)
    task_type = _get_type_task(task_config)
    logger.info(f"Beginning Task: identify associations ({task_type})")

    interim_path = Path(config.data.interim_data_path)
    output_path = Path(config.data.processed_data_path) / "identify_associations"
    output_path.mkdir(exist_ok=True, parents=True)

    # Read original data and create perturbed datasets
    logger.info(f"Perturbing dataset: '{task_config.target_dataset}'")
    cat_list, cat_names, con_list, con_names = io.read_data(config)

    mappings = io.load_mappings(interim_path / "mappings.json")
    target_mapping = mappings[task_config.target_dataset]
    target_value = one_hot_encode_single(target_mapping, task_config.target_value)
    logger.debug(
        f"Target value: {task_config.target_value} => {target_value.astype(int)[0]}"
    )

    train_mask, train_dataloader = make_dataloader(
        cat_list,
        con_list,
        shuffle=True,
        batch_size=task_config.batch_size,
        drop_last=True,
    )
    logger.debug(f"Masked training samples: {np.sum(~train_mask)}/{train_mask.size}")

    con_shapes = [con.shape[1] for con in con_list]
    dataloaders = perturb_data(
        cat_list,
        con_list,
        config.data.categorical_names,
        task_config.target_dataset,
        target_value,
    )

    baseline_dataloader = dataloaders[-1]
    baseline_dataset = cast(MOVEDataset, baseline_dataloader.dataset)
    num_features = len(dataloaders) - 1  # F
    num_samples = len(cast(SequentialSampler, baseline_dataloader.sampler))  # N
    num_continuous = sum(con_shapes)  # C
    logger.debug(f"# perturbed features: {num_features}")
    logger.debug(f"# continuous features: {num_continuous}")

    assert baseline_dataset.con_all is not None
    orig_con = baseline_dataset.con_all
    nan_mask = (orig_con == 0).numpy()  # NaN values encoded as 0s
    logger.debug(f"# NaN values: {np.sum(nan_mask)}/{orig_con.numel()}")

    target_dataset_idx = config.data.categorical_names.index(task_config.target_dataset)
    target_dataset = cat_list[target_dataset_idx]
    feature_mask = np.all(target_dataset == target_value, axis=2)
    feature_mask |= np.sum(target_dataset, axis=2) == 0

    def _bayes_approach(task_config: IdentifyAssociationsBayesConfig) -> tuple[FloatArray, ...]:
        # Train models
        logger.info("Training models")
        mean_diff = np.zeros((num_features, num_samples, num_continuous))
        normalizer = 1 / task_config.num_refits
        for j in range(task_config.num_refits):

            # Initialize model
            model: VAE = hydra.utils.instantiate(
                task_config.model,
                continuous_shapes=baseline_dataset.con_shapes,
                categorical_shapes=baseline_dataset.cat_shapes,
            )
            if j == 0:
                logger.debug(f"Model: {model}")

            # Train model
            logger.debug(f"Training refit {j + 1}/{task_config.num_refits}")
            _ = hydra.utils.call(
                task_config.training_loop,
                model=model,
                train_dataloader=train_dataloader,
            )
            model.eval()

            # Calculate baseline reconstruction
            _, baseline_recon = model.reconstruct(baseline_dataloader)
            # Calculate perturb reconstruction => keep track of mean difference
            for i in range(num_features):
                _, perturb_recon = model.reconstruct(dataloaders[i])
                diff = perturb_recon - baseline_recon  # 2D: N x C
                mean_diff[i, :, :] += diff * normalizer

        # Calculate Bayes factors
        logger.info("Identifying significant features")
        bayes_k = np.empty((num_features, num_continuous))
        for i in range(num_features):
            mask = feature_mask[:, [i]] | nan_mask  # 2D: N x C
            diff = np.ma.masked_array(mean_diff[i, :, :], mask=mask)  # 2D: N x C
            prob = np.ma.compressed(np.mean(diff > 1e-8, axis=0))  # 1D: C
            bayes_k[i, :] = np.abs(np.log(prob + 1e-8) - np.log(1 - prob + 1e-8))

        # Calculate Bayes probabilities
        bayes_p = np.exp(bayes_k) / (1 + np.exp(bayes_k))
        sort_ids = np.argsort(bayes_k, axis=None)[::-1]
        prob = np.take(bayes_p, sort_ids)  # 1D: N x C
        logger.debug(f"Bayes proba range: [{prob[-1]:.3f} {prob[0]:.3f}]")

        # Calculate FDR
        fdr = np.cumsum(1 - prob) / np.arange(1, prob.size + 1)
        idx = np.argmin(np.abs(fdr - task_config.sig_threshold))
        logger.debug(f"FDR range: [{fdr[0]:.3f} {fdr[-1]:.3f}]")

        return sort_ids[:idx], prob[:idx]

    def _ttest_approach(task_config: IdentifyAssociationsTTestConfig) -> tuple[FloatArray, ...]:
        from scipy.stats import ttest_rel

        # Train models
        logger.info("Training models")
        pvalues = np.empty(
            (
                len(task_config.num_latent),
                task_config.num_refits,
                num_features,
                num_continuous,
            )
        )

        for k, num_latent in enumerate(task_config.num_latent):
            for j in range(task_config.num_refits):

                # Initialize model
                model: VAE = hydra.utils.instantiate(
                    task_config.model,
                    continuous_shapes=baseline_dataset.con_shapes,
                    categorical_shapes=baseline_dataset.cat_shapes,
                    num_latent=num_latent,
                )
                if j == 0:
                    logger.debug(f"Model: {model}")

                # Train model
                logger.debug(f"Training refit {j + 1}/{task_config.num_refits}")
                _ = hydra.utils.call(
                    task_config.training_loop,
                    model=model,
                    train_dataloader=train_dataloader,
                )
                model.eval()

                # Get baseline reconstruction and baseline difference
                _, baseline_recon = model.reconstruct(baseline_dataloader)
                baseline_diff = np.empty((10, num_samples, num_continuous))
                for i in range(10):
                    _, recon = model.reconstruct(baseline_dataloader)
                    baseline_diff[i, :, :] = recon - baseline_recon
                baseline_diff = np.mean(baseline_diff, axis=0)  # 2D: N x C
                baseline_diff = np.where(nan_mask, np.nan, baseline_diff)

                # T-test between baseline and perturb difference
                for i in range(num_features):
                    _, perturb_recon = model.reconstruct(dataloaders[i])
                    perturb_diff = perturb_recon - baseline_recon
                    mask = feature_mask[:, [i]] | nan_mask
                    _, pvalues[k, j, i, :] = ttest_rel(
                        a=np.where(mask, np.nan, perturb_diff),
                        b=np.where(mask, np.nan, baseline_diff),
                        axis=0,
                        nan_policy="omit",
                    )

        # Correct p-values (Bonferroni)
        pvalues = np.minimum(pvalues * num_continuous, 1.0)

        # Find significant hits
        overlap_thres = task_config.num_refits // 2
        reject = pvalues <= task_config.sig_threshold  # 4D: L x R x F x C
        overlap = reject.sum(axis=1) >= overlap_thres  # 3D: L x F x C
        sig_ids = overlap.sum(axis=0) >= 3  # 2D: F x C
        sig_ids = np.flatnonzero(sig_ids)  # 1D

        # Report median p-value
        masked_pvalues = np.ma.masked_array(pvalues, mask=~reject)  # 4D
        masked_pvalues = np.ma.median(masked_pvalues, axis=1)  # 3D
        masked_pvalues = np.ma.median(masked_pvalues, axis=0)  # 2D
        sig_pvalues = np.ma.compressed(np.take(masked_pvalues, sig_ids))  # 1D

        return sig_ids, sig_pvalues

    if task_type == "bayes":
        task_config = cast(IdentifyAssociationsBayesConfig, task_config)
        sig_ids, *extra_cols = _bayes_approach(task_config)
        extra_colnames = ["proba"]
    else:
        task_config = cast(IdentifyAssociationsTTestConfig, task_config)
        sig_ids, *extra_cols = _ttest_approach(task_config)
        extra_colnames = ["p_value"]

    # Prepare results
    logger.info(f"Significant hits found: {sig_ids.size}")

    if sig_ids.size > 0:
        sig_ids = np.vstack((sig_ids // num_continuous, sig_ids % num_continuous)).T

        logger.info("Writing results")
        results = pd.DataFrame(sig_ids, columns=["feature_a_id", "feature_b_id"])
        results.sort_values("feature_a_id", inplace=True)
        a_df = pd.DataFrame(dict(x=cat_names[target_dataset_idx])).reset_index()
        a_df.columns = ["feature_a_id", "feature_a_name"]  # type: ignore
        con_names = reduce(list.__add__, con_names)
        b_df = pd.DataFrame(dict(x=con_names)).reset_index()
        b_df.columns = ["feature_b_id", "feature_b_name"]  # type: ignore
        results = results.merge(a_df, on="feature_a_id").merge(b_df, on="feature_b_id")
        results["feature_b_dataset"] = pd.cut(
            results.feature_b_id,
            bins=np.cumsum([0] + con_shapes),
            right=False,
            labels=config.data.continuous_names,
        )
        for col, colname in zip(extra_cols, extra_colnames):
            results[colname] = col
        results.to_csv(output_path / "results_sig_assoc.tsv", sep="\t")
