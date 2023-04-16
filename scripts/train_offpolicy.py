import logging
import os
import time

import hydra
import torch
import wandb
from functorch import vmap
from omegaconf import OmegaConf

from omni_drones import CONFIG_PATH, init_simulation_app
from omni_drones.utils.torchrl import SyncDataCollector, AgentSpec
from omni_drones.utils.envs.transforms import (
    LogOnEpisode, FromMultiDiscreteAction, FromDiscreteAction
)
from omni_drones.utils.wandb import init_wandb

from setproctitle import setproctitle
from tensordict import TensorDict
from torchrl.envs.transforms import TransformedEnv, InitTracker, Compose

from tqdm import tqdm


@hydra.main(version_base=None, config_path=CONFIG_PATH, config_name="config")
def main(cfg):
    OmegaConf.resolve(cfg)
    OmegaConf.set_struct(cfg, False)
    simulation_app = init_simulation_app(cfg)
    run = init_wandb(cfg)
    setproctitle(run.name)
    print(OmegaConf.to_yaml(cfg))

    from omni_drones.envs.isaac_env import IsaacEnv
    from omni_drones.learning import SACPolicy, TD3Policy
    from omni_drones.learning.qmix import QMIX
    from omni_drones.learning.dqn import DQN
    from omni_drones.sensors.camera import Camera, PinholeCameraCfg

    policies = {"qmix": QMIX, "sac": SACPolicy, "dqn": DQN, "td3": TD3Policy}

    env_class = IsaacEnv.REGISTRY[cfg.task.name]
    base_env = env_class(cfg, headless=cfg.headless)

    def log(info):
        print(OmegaConf.to_yaml(info))
        run.log(info)

    info_keys = [
        k for k in base_env.observation_spec.keys(True, True) 
        if isinstance(k, tuple) and k[0]=="info"
    ]
    logger = LogOnEpisode(
        cfg.env.num_envs,
        in_keys=["return", "progress", *info_keys],
        log_keys=["return", "ep_length", *info_keys],
        logger_func=log,
    )

    transforms = [InitTracker(), logger]
    # optionally discretize the action space
    action_transform = cfg.task.get("action_transform", None)
    if action_transform is not None:
        if action_transform.startswith("multidiscrete"):
            nbins = int(action_transform.split(":")[1])
            transform = FromMultiDiscreteAction(("action", "drone.action"), nbins=nbins)
            transforms.append(transform)
        elif action_transform.startswith("discrete"):
            nbins = int(action_transform.split(":")[1])
            transform = FromDiscreteAction(("action", "drone.action"), nbins=nbins)
            transforms.append(transform)

    env = TransformedEnv(base_env, Compose(*transforms)).train()

    camera_cfg = PinholeCameraCfg(
        sensor_tick=0,
        resolution=(960, 720),
        data_types=["rgb"],
        usd_params=PinholeCameraCfg.UsdCameraCfg(
            focal_length=24.0,
            focus_distance=400.0,
            horizontal_aperture=20.955,
            clipping_range=(0.1, 1.0e5),
        ),
    )
    
    camera = Camera(camera_cfg)
    camera.spawn(["/World/Camera"], translations=[(7.5, 7.5, 7.5)], targets=[(0, 0, 0.5)])
    camera.initialize("/World/Camera")

    # TODO: create a agent_spec view for TransformedEnv
    agent_spec = AgentSpec(
        name=base_env.agent_spec["drone"].name,
        n=base_env.agent_spec["drone"].n,
        observation_spec=env.observation_spec["drone.obs"],
        action_spec=env.action_spec["drone.action"],
        reward_spec=env.reward_spec["drone.reward"],
        state_spec=env.observation_spec["drone.state"] if base_env.agent_spec["drone"].state_spec is not None else None,
    )

    policy = policies[cfg.algo.name](
        cfg.algo, agent_spec=agent_spec, device="cuda"
    )

    frames_per_batch = env.num_envs * int(cfg.algo.train_every)
    total_frames = cfg.get("total_frames", -1) // frames_per_batch * frames_per_batch
    collector = SyncDataCollector(
        env,
        policy=policy,
        frames_per_batch=frames_per_batch,
        total_frames=total_frames,
        device=cfg.sim.device,
        return_same_td=True,
    )

    @torch.no_grad()
    def evaluate():
        info = {"env_frames": collector._frames}
        frames = []

        def record_frame(*args, **kwargs):
            frame = camera.get_images()["rgb"][0]
            frames.append(frame.cpu())

        base_env.enable_render(True)
        env.rollout(
            max_steps=base_env.max_episode_length,
            policy=policy,
            callback=record_frame,
            auto_reset=True,
            break_when_any_done=False,
        )
        base_env.enable_render(not cfg.headless)

        info["recording"] = wandb.Video(
            torch.stack(frames).permute(0, 3, 1, 2), fps=1 / cfg.sim.dt, format="mp4"
        )
        return info

    pbar = tqdm(collector)
    for i, data in enumerate(pbar):        
        info = {"env_frames": collector._frames}
        info.update(policy.train_op(data))

        run.log(info)
        print(OmegaConf.to_yaml(info))

        # if i % 100 == 0:
        #     logging.info(f"Eval at {collector._frames} steps.")
        #     run.log(evaluate())

        pbar.set_postfix({
            "rollout_fps": collector._fps,
            "frames": collector._frames,
        })

    logging.info(f"Final Eval at {collector._frames} steps.")
    info = {"env_frames": collector._frames}
    info.update(evaluate())
    run.log(info)

    simulation_app.close()


if __name__ == "__main__":
    main()
