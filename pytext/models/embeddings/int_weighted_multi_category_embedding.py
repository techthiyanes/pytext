#!/usr/bin/env python3
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved
from typing import Dict, List, Tuple, Optional

import torch
import torch.nn as nn
from pytext.config.module_config import ModuleConfig
from pytext.utils.usage import log_class_usage

from .embedding_base import EmbeddingBase


class IntWeightedMultiCategoryEmbedding(EmbeddingBase):
    """Embed Dict of feature_id -> (feature values, offsets, weights) to list of tensors (1 tensor per feature ID) with EmbeddingBag,
    then apply optional pooling and MLP to final tensor.
    Passed in feature dict keys need to be in fixed order in forward.
    """

    class Config(ModuleConfig):
        embedding_dim: int = 32
        weight_scale: float = 1.0
        # Deprecated, use features_embedding_bag_mode instead.
        embedding_bag_mode: Optional[str] = None
        # Deprecated, use features_embedding_bag_mode instead.
        ignore_weight: Optional[bool] = None

        # for every feaure in feature_buckets, configure its embedding bag (If not configured, by default it's `sum`).
        # If embedding bag is mean/max, its weight will be ignored.
        features_embedding_bag_mode: Dict[int, str] = {}
        # mean / max / none (concat)
        pooling_type: str = "none"
        # Apply MLP layers after pooling.
        mlp_layer_dims: List[int] = []
        # Per feature buckets, emb bucket = mod(feature_value, feature_buckets[feature_id]).
        # When pooling_type is none, the concat order is based on the key order.
        feature_buckets: Dict[int, int] = {}

    @classmethod
    def from_config(cls, config: Config):
        """Factory method to construct an instance of IntWeightedMultiCategoryEmbedding
        from the module's config object and the field's metadata object.

        Args:
            config (Config): Configuration object specifying all the
            parameters of IntWeightedMultiCategoryEmbedding.
            num_intput_features: Number of input features in forward.

        Returns:
            type: An instance of IntWeightedMultiCategoryEmbedding.

        """
        return cls(
            embedding_dim=config.embedding_dim,
            weight_scale=config.weight_scale,
            features_embedding_bag_mode=config.features_embedding_bag_mode,
            embedding_bag_mode=config.embedding_bag_mode,
            ignore_weight=config.ignore_weight,
            pooling_type=config.pooling_type,
            mlp_layer_dims=config.mlp_layer_dims,
            feature_buckets=config.feature_buckets,
        )

    def __init__(
        self,
        embedding_dim: int,
        weight_scale: float,
        features_embedding_bag_mode: Dict[int, str],
        embedding_bag_mode: Optional[str],
        ignore_weight: Optional[bool],
        pooling_type: str,
        mlp_layer_dims: List[int],
        feature_buckets: Dict[int, int],
    ) -> None:
        super().__init__(embedding_dim)

        features_embedding_bag_mode = {
            int(k): v for k, v in features_embedding_bag_mode.items()
        }
        if (
            ignore_weight is not None or embedding_bag_mode is not None
        ):  # for back-compatibility.
            assert (
                len(features_embedding_bag_mode) == 0
            ), "ignore_weight could only be set in old config and couldn't be set together with features_embedding_bag_mode"
            ignore_weight = ignore_weight or False
            embedding_bag_mode = embedding_bag_mode or "sum"
            features_embedding_bag_mode = {
                int(k): embedding_bag_mode if ignore_weight else "sum"
                for k in feature_buckets.keys()
            }

        self.weight_scale = weight_scale
        self.features_embedding_bag_mode = features_embedding_bag_mode
        self.pooling_type = pooling_type
        self.mlp_layer_dims = mlp_layer_dims

        self.feature_buckets = {int(k): v for k, v in feature_buckets.items()}
        self.feature_embeddings = nn.ModuleDict(
            {
                str(k): nn.EmbeddingBag(
                    v,
                    embedding_dim,
                    mode=self.features_embedding_bag_mode.get(k, "sum"),
                )
                for k, v in feature_buckets.items()
            }
        )

        self.num_intput_features = len(feature_buckets)
        input_dim = (
            self.num_intput_features * embedding_dim
            if self.pooling_type == "none"
            else embedding_dim
        )
        self.mlp = nn.Sequential(
            *(
                nn.Sequential(nn.Linear(m, n), nn.ReLU())
                for m, n in zip(
                    [input_dim] + list(mlp_layer_dims),
                    mlp_layer_dims,
                )
            )
        )
        log_class_usage(__class__)

    def get_output_dim(self):
        if self.mlp_layer_dims:
            return self.mlp_layer_dims[-1]

        if self.pooling_type == "none":
            return self.num_intput_features * self.embedding_dim
        elif self.pooling_type == "mean":
            return self.embedding_dim
        elif self.pooling_type == "max":
            return self.embedding_dim
        else:
            raise RuntimeError(f"Pooling type {self.pooling_type} is unsupported.")

    def forward(
        self, feats: Dict[int, Tuple[torch.Tensor, torch.Tensor, torch.Tensor]]
    ) -> torch.Tensor:
        embeddings: List[torch.Tensor] = []
        for k, buckets in self.feature_buckets.items():
            # it will throw if no key found in feats
            (feat, offsets, weights) = feats[k]
            feats_remap = torch.remainder(feat, buckets)
            feat_emb: nn.EmbeddingBag = self.feature_embeddings[str(k)]
            embeddings.append(
                feat_emb(
                    feats_remap,
                    offsets=offsets,
                    per_sample_weights=(
                        weights
                        if self.features_embedding_bag_mode.get(k, "sum") == "sum"
                        else None
                    ),
                )
                * self.weight_scale
            )

        if self.pooling_type == "none":  # None
            reduced_embeds = torch.cat(embeddings, dim=1)
        elif self.pooling_type == "mean":
            reduced_embeds = torch.sum(torch.stack(embeddings, dim=1), dim=1)
        elif self.pooling_type == "max":
            reduced_embeds, _ = torch.max(torch.stack(embeddings, dim=1), dim=1)
        else:
            raise RuntimeError(f"Pooling type {self.pooling_type} is unsupported.")

        return self.mlp(reduced_embeds)
