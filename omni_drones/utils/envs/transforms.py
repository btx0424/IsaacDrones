from collections import Callable, defaultdict
from typing import Any, Dict, Sequence, Union

import torch
from tensordict.tensordict import TensorDictBase
from torchrl.envs.transforms import Transform
from torchrl.data import (
    TensorSpec,
    BoundedTensorSpec,
    DiscreteTensorSpec,
    MultiDiscreteTensorSpec,
    CompositeSpec,
)


class LogOnEpisode(Transform):
    def __init__(
        self,
        n_episodes: int,
        in_keys: Sequence[str] = None,
        log_keys: Sequence[str] = None,
        logger_func: Callable = None,
        process_func: Dict[str, Callable] = None,
    ):
        super().__init__(in_keys=in_keys)
        if not len(in_keys) == len(log_keys):
            raise ValueError
        self.in_keys = in_keys
        self.log_keys = log_keys

        self.n_episodes = n_episodes
        self.logger_func = logger_func
        self.process_func = defaultdict(lambda: lambda x: torch.mean(x).item())
        if process_func is not None:
            self.process_func.update(process_func)

        self.stats = []

    def _call(self, tensordict: TensorDictBase) -> TensorDictBase:
        return tensordict

    def _step(self, tensordict: TensorDictBase) -> TensorDictBase:
        _reset = tensordict.get(
            ("next", "done"),
            torch.ones(
                tensordict.batch_size,
                dtype=torch.bool,
                device=tensordict.device,
            ),
        ).squeeze(-1)
        if _reset.any():
            self.stats.extend(
                tensordict[_reset].select(*self.in_keys).clone().unbind(0)
            )
            if len(self.stats) >= self.n_episodes:
                stats: TensorDictBase = torch.stack(self.stats)
                dict_to_log = {}
                for in_key, log_key in zip(self.in_keys, self.log_keys):
                    if in_key in stats.keys():
                        process_func = self.process_func[in_key]
                        dict_to_log[log_key] = process_func(stats[in_key])
                if self.logger_func is not None:
                    self.logger_func(dict_to_log)
                self.stats.clear()
        return tensordict


class FromDiscreteAction(Transform):
    def __init__(
        self,
        action_key: Sequence[str] = None,
        nbins: Union[int, Sequence[int]] = None,
    ):
        if action_key is None:
            action_key = "action"
        if nbins is None:
            nbins = 2
        super().__init__([], in_keys_inv=[action_key])
        self.nbins = nbins
        self.action_key = action_key

    def transform_input_spec(self, input_spec: CompositeSpec) -> CompositeSpec:
        action_spec = input_spec[self.action_key]
        if isinstance(action_spec, BoundedTensorSpec):
            if isinstance(self.nbins, int):
                nbins = [self.nbins] * action_spec.shape[-1]
            elif len(self.nbins) == action_spec.shape[-1]:
                nbins = self.nbins
            else:
                raise ValueError(
                    "nbins must be int or list of length equal to the last dimension of action space."
                )
            self.minimum = action_spec.space.minimum.unsqueeze(-2)
            self.maximum = action_spec.space.maximum.unsqueeze(-2)
            self.mapping = torch.cartesian_prod(
                *[torch.linspace(0, 1, dim_nbins) for dim_nbins in nbins]
            ).to(action_spec.device)  # [prod(nbins), len(nbins)]
            n = self.mapping.shape[0]
            spec = DiscreteTensorSpec(
                n, shape=[*action_spec.shape[:-1], 1], device=action_spec.device
            )
            input_spec[self.action_key] = spec
        else:
            NotImplementedError("Only BoundedTensorSpec is supported.")
        return input_spec

    def _inv_apply_transform(self, action: torch.Tensor) -> torch.Tensor:
        mapping = self.mapping * (self.maximum - self.minimum) + self.minimum
        action = action.unsqueeze(-1).unsqueeze(-1)
        action = torch.take_along_dim(mapping, action, dim=-2).squeeze(-2)
        return action


class FromMultiDiscreteAction(Transform):
    def __init__(
        self,
        action_key: Sequence[str] = None,
        nbins: Union[int, Sequence[int]] = 2,
    ):
        if action_key is None:
            action_key = "action"
        super().__init__([], in_keys_inv=[action_key])
        self.nbins = nbins
        self.action_key = action_key

    def transform_input_spec(self, input_spec: CompositeSpec) -> CompositeSpec:
        action_spec = input_spec[self.action_key]
        if isinstance(action_spec, BoundedTensorSpec):
            if isinstance(self.nbins, int):
                nbins = [self.nbins] * action_spec.shape[-1]
            elif len(self.nbins) == action_spec.shape[-1]:
                nbins = self.nbins
            else:
                raise ValueError(
                    "nbins must be int or list of length equal to the last dimension of action space."
                )
            spec = MultiDiscreteTensorSpec(
                nbins, shape=action_spec.shape, device=action_spec.device
            )
            self.nvec = spec.nvec.to(action_spec.device)
            self.minimum = action_spec.space.minimum
            self.maximum = action_spec.space.maximum
            input_spec[self.action_key] = spec
        else:
            NotImplementedError("Only BoundedTensorSpec is supported.")
        return input_spec

    def _inv_apply_transform(self, action: torch.Tensor) -> torch.Tensor:
        action = action / (self.nvec - 1) * (self.maximum - self.minimum) + self.minimum
        return action

    def _inv_call(self, tensordict: TensorDictBase) -> TensorDictBase:
        return super()._inv_call(tensordict)