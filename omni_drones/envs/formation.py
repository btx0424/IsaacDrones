from functorch import vmap

import omni.isaac.core.utils.torch as torch_utils
import omni_drones.utils.kit as kit_utils
import omni_drones.utils.scene as scene_utils
import torch

from omni_drones.envs.isaac_env import AgentSpec, IsaacEnv, List, Optional
from omni_drones.utils.torch import cpos, off_diag, others, make_cells
from omni_drones.robots.config import RobotCfg
from omni_drones.robots.drone import MultirotorBase
from tensordict.tensordict import TensorDict, TensorDictBase
from torchrl.data import CompositeSpec, UnboundedContinuousTensorSpec

REGULAR_HEXAGON = [
    [0, 0, 0],
    [1.7321, -1, 0],
    [0, -2, 0],
    [-1.7321, -1, 0],
    [-1.7321, 1.0, 0],
    [0.0, 2.0, 0.0],
    [1.7321, 1.0, 0.0],
]

REGULAR_TETRAGON = [
    [0, 0, 0],
    [1, 1, 0],
    [1, -1, 0],
    [-1, -1, 0],
    [-1, 1, 0],
]

FORMATIONS = {
    "hexagon": REGULAR_HEXAGON,
    "tetragon": REGULAR_TETRAGON,
}

def sample_from_grid(cells: torch.Tensor, n):
    idx = torch.randperm(cells.shape[0], device=cells.device)[:n]
    return cells[idx]

class Formation(IsaacEnv):
    def __init__(self, cfg, headless):
        super().__init__(cfg, headless)
        self.time_encoding = self.cfg.task.time_encoding
        self.safe_distance = self.cfg.task.safe_distance

        self.drone.initialize()
        self.init_poses = self.drone.get_world_poses(clone=True)

        obs_self_dim = self.drone.state_spec.shape[0]
        if self.time_encoding:
            self.time_encoding_dim = 4
            obs_self_dim += self.time_encoding_dim

        observation_spec = CompositeSpec({
            "obs_self": UnboundedContinuousTensorSpec((1, obs_self_dim)).to(self.device),
            "obs_others": UnboundedContinuousTensorSpec((self.drone.n-1, 13+1)).to(self.device),
        })

        state_spec = CompositeSpec(
            {
                "drones": self.drone.state_spec.expand(
                    self.drone.n, *self.drone.state_spec.shape
                ).to(self.device),
            }
        )

        self.agent_spec["drone"] = AgentSpec(
            "drone",
            5,
            observation_spec,
            self.drone.action_spec.to(self.device),
            UnboundedContinuousTensorSpec(1).to(self.device),
            state_spec,
        )

        # initial state distribution
        self.cells = make_cells([-2, -2, 0.6], [2, 2, 2], [0.5, 0.5, 0.2], device=self.device).flatten(0, -2)
        self.target_pos = self.target_pos.expand(self.num_envs, 1, 3)
        
        # additional infos & buffers
        stats_spec = CompositeSpec({
            "cost_laplacian": UnboundedContinuousTensorSpec((self.num_envs, 1)),
            "cost_hausdorff": UnboundedContinuousTensorSpec((self.num_envs, 1)),
        }, shape=[self.num_envs]).to(self.device)
        self.observation_spec["stats"] = stats_spec

        self.stats = stats_spec.zero()

        self.last_cost_l = torch.zeros(self.num_envs, 1, device=self.device)
        self.last_cost_h = torch.zeros(self.num_envs, 1, device=self.device)
        self.last_cost_pos = torch.zeros(self.num_envs, 1, device=self.device)

    def _design_scene(self) -> Optional[List[str]]:
        drone_model = MultirotorBase.REGISTRY[self.cfg.task.drone_model]
        cfg = drone_model.cfg_cls(force_sensor=self.cfg.task.force_sensor)
        self.drone: MultirotorBase = drone_model(cfg=cfg)

        scene_utils.design_scene()

        self.target_pos = torch.tensor([0.0, 0.0, 1.5], device=self.device)
        
        if isinstance(self.cfg.task.formation, str):
            self.formation = torch.as_tensor(
                FORMATIONS["tetragon"], device=self.device
            ).float()
        elif isinstance(self.cfg.task.formation, list):
            self.formation = torch.as_tensor(
                self.cfg.task.formation, device=self.device
            )
        else:
            raise ValueError(f"Invalid target formation {self.cfg.task.formation}")

        self.formation = self.formation + self.target_pos
        self.formation_L = laplacian(self.formation)

        self.drone.spawn(translations=self.formation)
        return ["/World/defaultGroundPlane"]

    def _reset_idx(self, env_ids: torch.Tensor):
        _, rot = self.init_poses
        self.drone._reset_idx(env_ids)
        
        pos = vmap(sample_from_grid, randomness="different")(
            self.cells.expand(len(env_ids), *self.cells.shape), n=self.drone.n
        ) + self.envs_positions[env_ids].unsqueeze(1)
        vel = torch.zeros(len(env_ids), self.drone.n, 6, device=self.device)
        self.drone.set_world_poses(pos, rot[env_ids], env_ids)
        self.drone.set_velocities(vel, env_ids)

        self.last_cost_h[env_ids] = vmap(cost_formation_laplacian)(
            pos, desired_L=self.formation_L
        )
        self.last_cost_l[env_ids] = vmap(cost_formation_hausdorff)(
            pos, desired_p=self.formation
        )
        com_pos = (pos - self.envs_positions[env_ids].unsqueeze(1)).mean(1, keepdim=True)
        self.last_cost_pos[env_ids] = torch.square(
            com_pos - self.target_pos[env_ids]
        ).sum(2)

        self.stats[env_ids] = 0.

    def _pre_sim_step(self, tensordict: TensorDictBase):
        actions = tensordict[("action", "drone.action")]
        self.effort = self.drone.apply_action(actions)

    def _compute_state_and_obs(self):
        self.root_states = self.drone.get_state()
        pos = self.drone.pos
        self.root_states[..., :3] = self.target_pos - pos

        obs_self = [self.root_states]
        if self.time_encoding:
            t = (self.progress_buf / self.max_episode_length).reshape(-1, 1, 1)
            obs_self.append(t.expand(-1, self.drone.n, self.time_encoding_dim))
        obs_self = torch.cat(obs_self, dim=-1)

        relative_pos = vmap(cpos)(pos, pos)
        self.drone_pdist = vmap(off_diag)(torch.norm(relative_pos, dim=-1, keepdim=True))
        relative_pos = vmap(off_diag)(relative_pos)

        obs_others = torch.cat([
            relative_pos,
            self.drone_pdist,
            vmap(others)(self.root_states[..., 3:13])
        ], dim=-1)

        obs = TensorDict({
            "obs_self": obs_self.unsqueeze(2),
            "obs_others": obs_others,
        }, [self.num_envs, self.drone.n])

        state = TensorDict({"drones": self.root_states}, self.batch_size)

        return TensorDict({
            "drone.obs": obs, 
            "drone.state": state,
        }, self.batch_size)

    def _compute_reward_and_done(self):
        pos, rot = self.get_env_poses(self.drone.get_world_poses())

        cost_l = vmap(cost_formation_laplacian)(pos, desired_L=self.formation_L)
        cost_h = vmap(cost_formation_hausdorff)(pos, desired_p=self.formation)
        
        cost_pos = torch.square(pos.mean(-2, keepdim=True) - self.target_pos).sum(-1)

        # reward_formation =  1 / (1 + torch.square(cost_h * 1.6)) 
        # reward_pos = 1 / (1 + cost_pos)

        reward_formation = torch.exp(- cost_h * 1.6)
        reward_pos = torch.exp(- cost_pos)

        separation = self.drone_pdist.min(dim=-2).values.min(dim=-2).values
        reward_separation = torch.square(separation / self.safe_distance).clamp(0, 1)
        reward = (
            reward_separation * (
                reward_formation 
                + reward_formation * reward_pos
                + 0.2 * reward_pos
            )
        ).unsqueeze(1).expand(-1, self.drone.n, 1)

        self.last_cost_l[:] = cost_l
        self.last_cost_h[:] = cost_h
        self.last_cost_pos[:] = cost_pos

        self._tensordict["return"] += reward

        terminated = (self.progress_buf >= self.max_episode_length).unsqueeze(-1)
        crash = (pos[..., 2] < 0.2).any(-1, keepdim=True)

        done = terminated | crash

        self.stats["cost_laplacian"] -= cost_l
        self.stats["cost_hausdorff"] -= cost_h

        return TensorDict(
            {
                "reward": {"drone.reward": reward},
                "return": self._tensordict["return"],
                "done": done,
                "stats": self.stats,
            },
            self.batch_size,
        )


def cost_formation_laplacian(
    p: torch.Tensor,
    desired_L: torch.Tensor,
    normalized=False,
) -> torch.Tensor:
    """
    A scale and translation invariant formation similarity cost
    """
    L = laplacian(p, normalized)
    cost = torch.linalg.matrix_norm(desired_L - L)
    return cost.unsqueeze(-1)


def laplacian(p: torch.Tensor, normalize=False):
    """
    symmetric normalized laplacian

    p: (n, dim)
    """
    assert p.dim() == 2
    A = torch.cdist(p, p)
    D = torch.sum(A, dim=-1)
    if normalize:
        DD = D**-0.5
        A = torch.einsum("i,ij->ij", DD, A)
        A = torch.einsum("ij,j->ij", A, DD)
        L = torch.eye(p.shape[0], device=p.device) - A
    else:
        L = D - A
    return L


def cost_formation_hausdorff(p: torch.Tensor, desired_p: torch.Tensor) -> torch.Tensor:
    p = p - p.mean(-2, keepdim=True)
    desired_p = desired_p - desired_p.mean(-2, keepdim=True)
    cost = torch.max(directed_hausdorff(p, desired_p), directed_hausdorff(desired_p, p))
    return cost.unsqueeze(-1)


def directed_hausdorff(p: torch.Tensor, q: torch.Tensor) -> torch.Tensor:
    """
    p: (*, n, dim)
    q: (*, m, dim)
    """
    d = torch.cdist(p, q, p=2).min(-1).values.max(-1).values
    return d
