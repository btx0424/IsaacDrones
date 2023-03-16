import time
from typing import Any, Dict, List, Optional, Tuple, Union

import functorch
import torch
import torch.nn as nn
import torch.nn.functional as F
from tensordict import TensorDict
from tensordict.nn import make_functional, TensorDictModule
from torch.optim import lr_scheduler

from torchrl.data import (
    BoundedTensorSpec,
    CompositeSpec,
    MultiDiscreteTensorSpec,
    TensorSpec,
    UnboundedContinuousTensorSpec as UnboundedTensorSpec,
)

from omni_drones.envs.isaac_env import AgentSpec

from .utils import valuenorm
from .utils.clip_grad import clip_grad_norm_
from .utils.gae import compute_gae

LR_SCHEDULER = lr_scheduler._LRScheduler


class MAPPOPolicy(object):
    def __init__(
        self, cfg, agent_spec: AgentSpec, act_name: str = None, device="cuda"
    ) -> None:
        super().__init__()

        self.cfg = cfg
        self.agent_spec = agent_spec
        self.device = device

        self.clip_param = cfg.clip_param
        self.ppo_epoch = int(cfg.ppo_epochs)
        self.num_minibatches = int(cfg.num_minibatches)
        self.normalize_advantages = cfg.normalize_advantages

        self.entropy_coef = cfg.entropy_coef
        self.gae_gamma = cfg.gamma
        self.gae_lambda = cfg.gae_lambda

        self.act_dim = agent_spec.action_spec.shape.numel()

        if cfg.reward_weights is not None:
            self.reward_weights = torch.as_tensor(cfg.reward_weights, device=device)
        else:
            self.reward_weights = torch.ones(
                self.agent_spec.reward_spec.shape, device=device
            )

        self.obs_name = f"{self.agent_spec.name}.obs"
        self.act_name = act_name or f"{self.agent_spec.name}.action"
        self.state_name = f"{self.agent_spec.name}.state"
        self.reward_name = f"{self.agent_spec.name}.reward"

        self.make_actor()
        self.make_critic()

        self.train_in_keys = list(
            set(
                self.actor_in_keys
                + self.actor_out_keys
                + self.critic_in_keys
                + self.critic_out_keys
                + [
                    "next",
                    self.act_logps_name,
                    ("reward", self.reward_name),
                    "state_value",
                ]
                + ["progress", ("collector", "traj_ids")]
            )
        )

        self.n_updates = 0

    @property
    def act_logps_name(self):
        return f"{self.agent_spec.name}.action_logp"

    def make_actor(self):
        cfg = self.cfg.actor

        self.actor_in_keys = [self.obs_name, self.act_name]
        self.actor_out_keys = [
            self.act_name,
            self.act_logps_name,
            f"{self.agent_spec.name}.action_entropy",
        ]

        if self.cfg.share_actor:
            actor = make_ppo_actor(
                cfg, self.agent_spec.observation_spec, self.agent_spec.action_spec
            )

            if actor.rnn:
                self.actor_in_keys.extend(
                    [f"{self.agent_spec.name}.rnn_state", "is_init"]
                )
                self.actor_out_keys.append(f"{self.agent_spec.name}.rnn_state")
                self.minibatch_seq_len = self.cfg.actor.rnn.train_seq_len
                assert self.minibatch_seq_len <= self.cfg.train_every
            
            self.actor = TensorDictModule(
                actor, in_keys=self.actor_in_keys, out_keys=self.actor_out_keys
            ).to(self.device)
            self.actor_opt = torch.optim.Adam(self.actor.parameters())
            self.actor_params_list = self.actor.parameters() # for grad clipping
            self.actor_params = make_functional(self.actor)
        else:
            raise NotImplementedError

    def make_critic(self):
        cfg = self.cfg.critic

        if cfg.use_huber_loss:
            self.critic_loss_fn = nn.HuberLoss(reduction="none", delta=cfg.huber_delta)
        else:
            self.critic_loss_fn = nn.MSELoss(reduction="none")

        if self.cfg.critic_input == "state":
            if self.agent_spec.state_spec is None:
                raise ValueError
            self.critic_in_keys = [f"{self.agent_spec.name}.state"]
            self.critic_out_keys = ["state_value"]

            self.critic = TensorDictModule(
                CentralizedCritic(
                    cfg,
                    entity_ids=torch.arange(self.agent_spec.n, device=self.device),
                    state_spec=self.agent_spec.state_spec,
                    reward_spec=self.agent_spec.reward_spec,
                ),
                in_keys=self.critic_in_keys,
                out_keys=self.critic_out_keys,
            ).to(self.device)
            self.value_func = self.critic

        elif self.cfg.critic_input == "obs":
            self.critic_in_keys = [f"{self.agent_spec.name}.obs"]
            self.critic_out_keys = ["state_value"]

            self.critic = TensorDictModule(
                SharedCritic(
                    cfg, self.agent_spec.observation_spec, self.agent_spec.reward_spec
                ),
                in_keys=self.critic_in_keys,
                out_keys=self.critic_out_keys,
            ).to(self.device)
            self.value_func = functorch.vmap(self.critic, in_dims=1, out_dims=1)
        else:
            raise ValueError(self.cfg.critic_input)

        self.critic_opt = torch.optim.Adam(
            self.critic.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay
        )
        scheduler = cfg.lr_scheduler
        if scheduler is not None:
            scheduler = eval(scheduler)
            self.critic_opt_scheduler: LR_SCHEDULER = scheduler(
                self.critic_opt, **cfg.lr_scheduler_kwargs
            )

        if hasattr(cfg, "value_norm") and cfg.value_norm is not None:
            # The original MAPPO implementation uses ValueNorm1 with a very large beta,
            # and normalizes advantages at batch level.
            # Tianshou (https://github.com/thu-ml/tianshou) uses ValueNorm2 with subtract_mean=False,
            # and normalizes advantages at mini-batch level.
            # Empirically the performance is similar on most of the tasks.
            cls = getattr(valuenorm, cfg.value_norm["class"])
            self.value_normalizer: valuenorm.Normalizer = cls(
                input_shape=self.agent_spec.reward_spec.shape,
                **cfg.value_norm["kwargs"],
            ).to(self.device)

    def value_op(self, tensordict: TensorDict) -> TensorDict:
        critic_input = tensordict.select(*self.critic_in_keys)
        critic_input.batch_size = [*critic_input.batch_size, self.agent_spec.n]
        tensordict = self.value_func(critic_input)
        return tensordict

    def __call__(self, tensordict: TensorDict, deterministic: bool = False):
        actor_input = tensordict.select(*self.actor_in_keys, strict=False)
        if "is_init" in actor_input.keys():
            actor_input["is_init"] = actor_input["is_init"].reshape(*actor_input.batch_size, self.agent_spec.n)
        actor_input.batch_size = [*actor_input.batch_size, self.agent_spec.n]
        actor_output = self.actor(
            actor_input.squeeze(1), self.actor_params, deterministic=deterministic
        )

        tensordict.update(actor_output.unsqueeze(1))
        tensordict.update(self.value_op(tensordict))
        return tensordict

    def update_actor(self, batch: TensorDict) -> Dict[str, Any]:
        advantages = batch["advantages"].squeeze(2)
        actor_input = batch.select(*self.actor_in_keys)
        actor_input.batch_size = [*actor_input.batch_size, self.agent_spec.n]

        log_probs_old = batch[self.act_logps_name].squeeze(2)
        if hasattr(self, "minibatch_seq_len"): # [N, T, A, *]
            actor_output = self.actor(
                actor_input.squeeze(2), self.actor_params, eval_action=True
            )
        else: # [N, A, *]
            actor_output = self.actor(
                actor_input.squeeze(1), self.actor_params, eval_action=True
            )

        log_probs = actor_output[self.act_logps_name]
        dist_entropy = actor_output[f"{self.agent_spec.name}.action_entropy"]
        assert advantages.shape == log_probs.shape == dist_entropy.shape

        ratio = torch.exp(log_probs - log_probs_old)
        surr1 = ratio * advantages
        surr2 = (
            torch.clamp(ratio, 1.0 - self.clip_param, 1.0 + self.clip_param)
            * advantages
        )
        policy_loss = - torch.mean(torch.min(surr1, surr2) * self.act_dim)
        entropy_loss = - torch.mean(dist_entropy)

        self.actor_opt.zero_grad()
        (policy_loss - entropy_loss * self.cfg.entropy_coef).backward()
        grad_norm = torch.nn.utils.clip_grad_norm_(self.actor_params_list, self.cfg.max_grad_norm)
        self.actor_opt.step()

        return {
            "policy_loss": policy_loss.item(),
            "actor_grad_norm": grad_norm.item(),
            "dist_entropy": entropy_loss.item(),
            # "clip_fraction": clip_frac.mean(),
            # "approx_kl": approx_kl.mean(),
        }

    def update_critic(self, batch: TensorDict) -> Dict[str, Any]:
        critic_input = batch.select(*self.critic_in_keys)
        values = self.value_op(critic_input)["state_value"]
        b_values = batch["state_value"]
        b_returns = batch["returns"]
        assert values.shape == b_values.shape == b_returns.shape
        value_pred_clipped = b_values + (values - b_values).clamp(
            -self.clip_param, self.clip_param
        )

        value_loss_clipped = self.critic_loss_fn(b_returns, value_pred_clipped)
        value_loss_original = self.critic_loss_fn(b_returns, values)

        value_loss = torch.max(value_loss_original, value_loss_clipped)

        value_loss.sum(-1).mean().backward()  # do not multiply weights here
        grad_norm = nn.utils.clip_grad_norm_(
            self.critic.parameters(), self.cfg.max_grad_norm
        )
        self.critic_opt.step()
        self.critic_opt.zero_grad(set_to_none=True)
        return {
            "value_loss": value_loss.mean(),
            "critic_grad_norm": grad_norm,
        }

    def _get_dones(self, tensordict: TensorDict):
        env_done = tensordict[("next", "done")].unsqueeze(-1)
        agent_done = tensordict.get(
            ("next", f"{self.agent_spec.name}.done"),
            env_done.expand(*env_done.shape[:-2], self.agent_spec.n, 1),
        )
        done = agent_done | env_done
        return done

    def train_op(self, tensordict: TensorDict):
        tensordict = tensordict.select(*self.train_in_keys, strict=False)
        next_tensordict = tensordict["next"][:, -1]
        with torch.no_grad():
            value_output = self.value_op(next_tensordict)

        rewards = tensordict.get(("next", "reward", f"{self.agent_spec.name}.reward"))

        values = tensordict["state_value"]
        next_value = value_output["state_value"].squeeze(0)

        if hasattr(self, "value_normalizer"):
            values = self.value_normalizer.denormalize(values)
            next_value = self.value_normalizer.denormalize(next_value)

        dones = self._get_dones(tensordict)

        tensordict["advantages"], tensordict["returns"] = compute_gae(
            rewards,
            dones,
            values,
            next_value,
            gamma=self.gae_gamma,
            lmbda=self.gae_lambda,
        )

        advantages_mean = tensordict["advantages"].mean()
        advantages_std = tensordict["advantages"].std()
        if self.normalize_advantages:
            tensordict["advantages"] = (tensordict["advantages"] - advantages_mean) / (
                advantages_std + 1e-8
            )

        if hasattr(self, "value_normalizer"):
            self.value_normalizer.update(tensordict["returns"])
            tensordict["returns"] = self.value_normalizer.normalize(
                tensordict["returns"]
            )

        train_info = []
        for ppo_epoch in range(self.ppo_epoch):
            dataset = make_dataset_naive(
                tensordict,
                self.cfg.num_minibatches,
                self.minibatch_seq_len if hasattr(self, "minibatch_seq_len") else 1,
            )
            for minibatch in dataset:
                train_info.append(
                    TensorDict(
                        {
                            **self.update_actor(minibatch),
                            **self.update_critic(minibatch),
                        },
                        batch_size=[],
                    )
                )

        train_info: TensorDict = torch.stack(train_info)
        train_info = train_info.apply(lambda x: x.mean(0), batch_size=[])
        train_info["advantages_mean"] = advantages_mean
        train_info["advantages_std"] = advantages_std
        train_info["action_norm"] = (
            tensordict[self.act_name].float().norm(dim=-1).mean()
        )
        if hasattr(self, "value_normalizer"):
            train_info["value_running_mean"] = self.value_normalizer.running_mean.mean()
        self.n_updates += 1
        return {f"{self.agent_spec.name}/{k}": v for k, v in train_info.items()}


def make_dataset_naive(
    tensordict: TensorDict, num_minibatches: int = 4, seq_len: int = 1
):
    if seq_len > 1:
        N, T = tensordict.shape
        T = (T // seq_len) * seq_len
        tensordict = tensordict[:, :T].reshape(-1, seq_len)
        perm = torch.randperm(
            (tensordict.shape[0] // num_minibatches) * num_minibatches,
            device=tensordict.device,
        ).reshape(num_minibatches, -1)
        for indices in perm:
            yield tensordict[indices]
    else:
        tensordict = tensordict.reshape(-1)
        perm = torch.randperm(
            (tensordict.shape[0] // num_minibatches) * num_minibatches,
            device=tensordict.device,
        ).reshape(num_minibatches, -1)
        for indices in perm:
            yield tensordict[indices]


from .utils.distributions import (
    DiagGaussian,
    IndependentBetaModule,
    IndependentNormalModule,
    MultiCategoricalModule,
)
from .utils.network import (
    ENCODERS_MAP,
    MLP,
    SplitEmbedding,
)

from .modules.rnn import GRU


def make_ppo_actor(cfg, observation_spec: TensorSpec, action_spec: TensorSpec):
    if isinstance(observation_spec, (BoundedTensorSpec, UnboundedTensorSpec)):
        if not len(observation_spec.shape) == 1:
            raise ValueError
        input_dim = observation_spec.shape[0]
        base = nn.Sequential(
            nn.LayerNorm(input_dim),
            MLP([input_dim] + cfg.hidden_units),
        )
        base.output_shape = torch.Size((cfg.hidden_units[-1],))
    elif isinstance(observation_spec, CompositeSpec):
        encoder_cls = ENCODERS_MAP[cfg.attn_encoder]
        base = encoder_cls(observation_spec)
    else:
        raise NotImplementedError(observation_spec)

    if isinstance(action_spec, MultiDiscreteTensorSpec):
        act_dist = MultiCategoricalModule(action_spec.nvec)
    elif isinstance(action_spec, (UnboundedTensorSpec, BoundedTensorSpec)):
        action_dim = action_spec.shape.numel()
        act_dist = DiagGaussian(base.output_shape.numel(), action_dim, True, 0.01)
        # act_dist = IndependentNormalModule(inputs_dim, action_dim, True)
        # act_dist = IndependentBetaModule(inputs_dim, action_dim)
    else:
        raise NotImplementedError(action_spec)

    if cfg.get("rnn", None):
        rnn_cls = {"gru": GRU}[cfg.rnn.cls.lower()]
        rnn = rnn_cls(input_size=base.output_shape.numel(), **cfg.rnn.kwargs)
    else:
        rnn = None

    return Actor(base, act_dist, rnn)


class Actor(nn.Module):
    def __init__(
        self,
        encoder: nn.Module,
        act_dist: nn.Module,
        rnn: Optional[nn.Module] = None,
    ) -> None:
        super().__init__()
        self.encoder = encoder
        self.act_dist = act_dist
        self.rnn = rnn

    def forward(
        self,
        obs: Union[torch.Tensor, TensorDict],
        action: torch.Tensor = None,
        rnn_state=None,
        is_init=None,
        deterministic=False,
        eval_action=False
    ):
        actor_features = self.encoder(obs)
        if self.rnn is not None:
            if actor_features.dim() == 2:  
                # single-step inference, unsqueeze to add time dimension
                rnn_features, rnn_state = self.rnn(
                    actor_features.unsqueeze(1), rnn_state, is_init
                )
                actor_features = actor_features + rnn_features.squeeze(1)
                rnn_state = rnn_state.squeeze(1)
            else:  
                # multi-step re-rollout during training
                rnn_features, rnn_state = self.rnn(
                    actor_features,
                    rnn_state[:, 0] if rnn_state is not None else None,
                    is_init,
                )
                actor_features = actor_features + rnn_features
        else:
            rnn_state = None
        action_dist = self.act_dist(actor_features)

        if eval_action:
            action_log_probs = action_dist.log_prob(action).unsqueeze(-1)
            dist_entropy = action_dist.entropy().unsqueeze(-1)
            return action, action_log_probs, dist_entropy, None
        else:
            action = action_dist.mode if deterministic else action_dist.sample()
            action_log_probs = action_dist.log_prob(action).unsqueeze(-1)
            return action, action_log_probs, None, rnn_state


class SharedCritic(nn.Module):
    def __init__(
        self,
        args,
        observation_spec: TensorSpec,
        reward_spec: TensorSpec,
    ):
        super().__init__()
        if isinstance(observation_spec, (BoundedTensorSpec, UnboundedTensorSpec)):
            input_dim = observation_spec.shape.numel()
            self.base = nn.Sequential(
                nn.LayerNorm(input_dim), MLP([input_dim] + args.hidden_units)
            )
            self.base.output_shape = torch.Size((args.hidden_units[-1],))
        elif isinstance(observation_spec, CompositeSpec):
            encoder_cls = ENCODERS_MAP[args.attn_encoder]
            self.base = encoder_cls(observation_spec)
        else:
            raise TypeError(observation_spec)

        self.output_shape = reward_spec.shape
        self.v_out = nn.Linear(
            self.base.output_shape.numel(), self.output_shape.numel()
        )
        nn.init.orthogonal_(self.v_out.weight, args.gain)

    def forward(self, obs: torch.Tensor):
        critic_features = self.base(obs)
        values = self.v_out(critic_features)
        if len(self.output_shape) > 1:
            values = values.unflatten(-1, self.output_shape)
        return values


INDEX_TYPE = Union[int, slice, torch.LongTensor, List[int]]


class CentralizedCritic(nn.Module):
    """Critic for centralized training.

    Args:
        entity_ids: indices of the entities that are considered as agents.

    """

    def __init__(
        self,
        cfg,
        entity_ids: INDEX_TYPE,
        state_spec: CompositeSpec,
        reward_spec: TensorSpec,
        embed_dim=128,
        nhead=1,
        num_layers=1,
    ):
        super().__init__()
        self.entity_ids = entity_ids
        self.embed = SplitEmbedding(state_spec, embed_dim=embed_dim)
        self.encoder = nn.TransformerEncoder(
            encoder_layer=nn.TransformerEncoderLayer(
                d_model=embed_dim,
                nhead=nhead,
                dim_feedforward=embed_dim,
                dropout=0.0,
                batch_first=True,
            ),
            num_layers=num_layers,
        )
        self.output_shape = reward_spec.shape
        self.v_out = nn.Linear(embed_dim, self.output_shape.shape.numel())

    def forward(self, x: torch.Tensor):
        x = self.embed(x)
        x = self.encoder(x)
        values = self.v_out(x[..., self.entity_ids, :]).unflatten(-1, self.output_shape)
        return values
