import logging
import os
import time

import hydra
import torch
import numpy as np
import wandb
from functorch import vmap
from omegaconf import OmegaConf

from omni_drones import CONFIG_PATH, init_simulation_app
from omni_drones.utils.torchrl import SyncDataCollector, AgentSpec
from omni_drones.utils.torchrl.transforms import (
    LogOnEpisode, 
    FromMultiDiscreteAction, 
    FromDiscreteAction,
    ravel_composite,
    VelController,
    AttitudeController,
    History
)
from omni_drones.utils.wandb import init_wandb
from omni_drones.learning import (
    PPOAdaptivePolicy, PPORNNPolicy, PPOPolicy, PPOTConvPolicy
)

from setproctitle import setproctitle
from torchrl.envs.transforms import (
    TransformedEnv, 
    InitTracker, 
    Compose,
    CatTensors
)

from tqdm import tqdm

class Every:
    def __init__(self, func, steps):
        self.func = func
        self.steps = steps
        self.i = 0

    def __call__(self, *args, **kwargs):
        if self.i % self.steps == 0:
            self.func(*args, **kwargs)
        self.i += 1

@hydra.main(config_path=CONFIG_PATH, config_name="train", version_base=None)
def main(cfg):
    OmegaConf.register_new_resolver("eval", eval)
    OmegaConf.resolve(cfg)
    OmegaConf.set_struct(cfg, False)
    simulation_app = init_simulation_app(cfg)
    run = init_wandb(cfg)
    setproctitle(run.name)
    print(OmegaConf.to_yaml(cfg))

    from omni_drones.envs.isaac_env import IsaacEnv
    algos = {
        "ppo": PPOPolicy,
        "ppo_adaptive": PPOAdaptivePolicy,
        "ppo_rnn": PPORNNPolicy,
        "ppo_tconv": PPOTConvPolicy,
    }
    env_class = IsaacEnv.REGISTRY[cfg.task.name]
    base_env = env_class(cfg, headless=cfg.headless)

    def log(info):
        print(OmegaConf.to_yaml(info))
        run.log(info)

    stats_keys = [
        k for k in base_env.observation_spec.keys(True, True) 
        if isinstance(k, tuple) and k[0]=="stats"
    ]
    logger = LogOnEpisode(
        cfg.env.num_envs,
        in_keys=stats_keys,
        log_keys=stats_keys,
        logger_func=log,
        # process_func={"return_std": lambda x: torch.std(x).item()}
    )
    transforms = [InitTracker(), logger]

    # a CompositeSpec is by deafault processed by a entity-based encoder
    # flatten it to use a MLP encoder instead
    if cfg.task.get("flatten_obs", False):
        transforms.append(ravel_composite(base_env.observation_spec, ("agents", "observation")))
    if cfg.task.get("flatten_state", False):
        transforms.append(ravel_composite(base_env.observation_spec, "state"))
    if (
        cfg.task.get("flatten_intrinsics", True)
        and ("agents", "intrinsics") in base_env.observation_spec.keys(True)
    ):
        transforms.append(ravel_composite(base_env.observation_spec, ("agents", "intrinsics"), start_dim=-1))

    # if cfg.task.get("history", False):
    #     # transforms.append(History([("info", "drone_state"), ("info", "prev_action")]))
    #     transforms.append(History([("agents", "observation")]))

    # optionally discretize the action space or use a controller
    action_transform: str = cfg.task.get("action_transform", None)
    if action_transform is not None:
        if action_transform.startswith("multidiscrete"):
            nbins = int(action_transform.split(":")[1])
            transform = FromMultiDiscreteAction(nbins=nbins)
            transforms.append(transform)
        elif action_transform.startswith("discrete"):
            nbins = int(action_transform.split(":")[1])
            transform = FromDiscreteAction(nbins=nbins)
            transforms.append(transform)
        elif action_transform == "velocity":
            from omni_drones.controllers import LeePositionController
            controller = LeePositionController(9.81, base_env.drone.params).to(base_env.device)
            transform = VelController(vmap(controller))
            transforms.append(transform)
        elif action_transform == "attitude":
            from omni_drones.controllers import AttitudeController as _AttitudeController
            controller = _AttitudeController(9.81, base_env.drone.params).to(base_env.device)
            transform = AttitudeController(vmap(vmap(controller)))
            transforms.append(transform)
        elif not action_transform.lower() == "none":
            raise NotImplementedError(f"Unknown action transform: {action_transform}")
    
    env = TransformedEnv(base_env, Compose(*transforms)).train()
    env.set_seed(cfg.seed)

    policy = algos[cfg.algo.name.lower()](
        cfg.algo, env.observation_spec, env.action_spec, env.reward_spec, device=base_env.device)

    frames_per_batch = env.num_envs * int(cfg.algo.train_every)
    total_frames = cfg.get("total_frames", -1) // frames_per_batch * frames_per_batch
    max_iters = cfg.get("max_iters", -1)
    eval_interval = cfg.get("eval_interval", -1)
    save_interval = cfg.get("save_interval", -1)


    @torch.no_grad()
    def evaluate():
        frames = []

        def record_frame(*args, **kwargs):
            frame = env.base_env.render(mode="rgb_array")
            frames.append(frame)

        base_env.enable_render(True)
        env.eval()
        env.rollout(
            max_steps=base_env.max_episode_length,
            policy=policy,
            callback=Every(record_frame, 2),
            auto_reset=True,
            break_when_any_done=False,
            return_contiguous=False
        )
        base_env.enable_render(not cfg.headless)
        env.reset()
        env.train()

        if len(frames):
            # video_array = torch.stack(frames)
            video_array = np.stack(frames).transpose(0, 3, 1, 2)
            info["recording"] = wandb.Video(
                video_array, fps=0.5 / cfg.sim.dt, format="mp4"
            )
        frames.clear()
        return info
    
    env.train()

    for i in range(3):
        info={}
        logging.info(f"Eval at {i} steps.")
        info.update(evaluate())
        run.log(info)
    

    try:
        ckpt_path = os.path.join(run.dir, "checkpoint_final.pt")
        logging.info(f"Save checkpoint to {str(ckpt_path)}")
        torch.save(policy.state_dict(), ckpt_path)
    except AttributeError:
        logging.warn(f"Policy {policy} does not implement `.state_dict()`")
        

    wandb.save(os.path.join(run.dir, "checkpoint*"))
    wandb.finish()
    
    simulation_app.close()


if __name__ == "__main__":
    main()
