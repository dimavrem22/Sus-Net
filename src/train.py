from enum import StrEnum, auto
from typing import Optional
import numpy as np
import pygame
import torch
import torch.nn.functional as F
import copy
import tqdm
import pathlib
from datetime import datetime
import json

from src.scheduler import ExponentialSchedule
from src.environment import FourRoomEnv, StateFields
from src.features.model_ready import SequenceStateFeaturizer, FeaturizerType
from src.metrics import EpisodicMetricHandler, SusMetrics
from src.replay_memory import ReplayBuffer
from src.models.dqn import ModelType, Q_Estimator
from src.visualize import AmongUsVisualizer
from src.utils import GeneralEncoder

BASE_REGISTRY_DIR = pathlib.Path(__file__).parent.parent / 'model_registry'

class OptimizerType(StrEnum):
    ADAM = auto()

    @staticmethod
    def build(optimizer_type: str, model: Q_Estimator, learning_rate: float):
        assert optimizer_type in [
            o.value for o in OptimizerType
        ], f"Invalid optimizer type: {optimizer_type}"
        if model.model_type == ModelType.RANDOM:
            return None
        if optimizer_type == OptimizerType.ADAM:
            return torch.optim.Adam(
                params=model.parameters(), lr=learning_rate
            )  # might need to make this more flexible in the future


class DQNTeamTrainer:

    def __init__(self, imposter_optimizer, crew_optimizer, gamma):
        self.imposter_optimizer = imposter_optimizer
        self.crew_optimizer = crew_optimizer
        self.gamma = gamma

        # whether or not this trainer is just a place holder!
        self.train = imposter_optimizer is not None or crew_optimizer is not None

    def train_step(
        self,
        batch,
        featurizer,
        imposter_model,
        imposter_target_model,
        crew_model,
        crew_target_model,
    ):

        accumulated_losses = [0, 0]

        if not self.train:
            return accumulated_losses

        # reset gradents for both optimizers
        for opt in [self.imposter_optimizer, self.crew_optimizer]:
            if opt is not None:
                opt.zero_grad()

        featurizer.fit(batch.states)
        featurized_state = featurizer.generate_featurized_states()

        featurizer.fit(batch.next_states)
        featurized_next_state = featurizer.generate_featurized_states()

        for agent_idx, (state_feat, next_state_feat) in enumerate(
            zip(featurized_state, featurized_next_state)
        ):

            # samples in which agnets is an imposter/crew member
            imposter_samples = (batch.imposters == agent_idx).view(-1)
            crew_samples = ~imposter_samples

            # spatial.requires_grad = non_spatial.requires_grad = True

            # training via gradient accumulation
            for loss_idx, (
                opt,
                team_samples,
                team_model,
                team_model_target,
            ) in enumerate(
                [
                    (
                        self.imposter_optimizer,
                        imposter_samples,
                        imposter_model,
                        imposter_target_model,
                    ),
                    (self.crew_optimizer, crew_samples, crew_model, crew_target_model),
                ]
            ):
                if opt is not None and team_samples.sum() > 0:
                    team_model.train()
                    # compute the value of the actions taken by the agent (gradients are calculated here!)

                    action_values = team_model(
                        state_feat[0][team_samples],
                        state_feat[1][team_samples],
                    )

                    actions = torch.tensor(batch.actions[team_samples, agent_idx])

                    values = torch.gather(action_values, 1, actions.view(-1, 1)).view(
                        -1
                    )

                    with torch.no_grad():
                        done_mask = batch.dones[team_samples].view(-1)

                        rewards = torch.tensor(
                            batch.rewards[team_samples, agent_idx]
                        ).view(-1)

                        # calculate target values, no gradients here (notice the detach() calls
                        target_values = (
                            rewards
                            + self.gamma
                            * torch.max(
                                team_model_target(
                                    next_state_feat[0][team_samples].detach(),
                                    next_state_feat[1][team_samples].detach(),
                                ),
                                dim=1,
                            )[0]
                        )
                        target_values[done_mask] = rewards[done_mask]

                    loss = F.mse_loss(values, target_values)
                    loss.backward()
                    accumulated_losses[loss_idx] += loss.item()

                    opt.step()

        # use gradients to update models
        # for opt in [self.imposter_optimizer, self.crew_optimizer]:
        #     if opt is not None:
        #         opt.step()
        return accumulated_losses


def run_experiment(
    env: FourRoomEnv,
    num_steps: int,
    imposter_model_args: dict,
    crew_model_args: dict,
    featurizer: SequenceStateFeaturizer,
    imposter_model_type: ModelType = ModelType.SPATIAL_DQN,
    crew_model_type: ModelType = ModelType.SPATIAL_DQN,
    sequence_length: int = 2,
    replay_buffer_size: int = 100_000,
    replay_prepopulate_steps: int = 1000,
    batch_size: int = 32,
    gamma: float = 0.99,
    scheduler_start_eps: float = 1.0,
    scheduler_end_eps: float = 0.05,
    scheduler_time_steps: int = 1_000_000,
    train_imposter: bool = True,
    train_crew: bool = True,
    experiment_base_dir: Optional[pathlib.Path] = None,
    optimizer_type: OptimizerType = OptimizerType.ADAM,
    learning_rate: float = 0.0001,
    train_step_interval: int = 5,
    num_checkpoint_saves: int = 5,
    target_update_interval: int = 10_000,
):
    # create a experiment dir
    if experiment_base_dir is None:        experiment_base_dir = BASE_REGISTRY_DIR / "experiments"
    
    # current time, gamma, learning rate
    experiment_dir = experiment_base_dir / f"{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}"
    experiment_dir.mkdir(parents=True, exist_ok=True)

    # save the experiment configuration
    experiment_config = {
        'num_steps': num_steps,
        'imposter_model_args': imposter_model_args,
        'crew_model_args': crew_model_args,
        'imposter_model_type': imposter_model_type,
        'crew_model_type': crew_model_type,
        'featurizer_type': str(featurizer),
        'sequence_length': sequence_length,
        'replay_buffer_size': replay_buffer_size,
        'replay_prepopulate_steps': replay_prepopulate_steps,
        'batch_size': batch_size,
        'gamma': gamma,
        'scheduler_start_eps': scheduler_start_eps,
        'scheduler_end_eps': scheduler_end_eps,
        'scheduler_time_steps': scheduler_time_steps,
        'train_imposter': train_imposter,
        'train_crew': train_crew,
        'experiment_base_dir': experiment_base_dir,
        'optimizer_type': optimizer_type,
        'learning_rate': learning_rate,
        'train_step_interval': train_step_interval,
        "target_update_interval": target_update_interval,
    }
    
    # save the configs
    with open(experiment_dir / 'config.json', "w") as f:
        json.dump(experiment_config, f, cls=GeneralEncoder, indent=4)

    # initializing models
    imposter_model = ModelType.build(imposter_model_type, **imposter_model_args)
    crew_model = ModelType.build(crew_model_type, **crew_model_args)

    # initializing optimizers
    crew_optimizer = imposter_optimizer = None
    if optimizer_type is not None:
        if train_imposter:
            imposter_optimizer = OptimizerType.build(
                optimizer_type, imposter_model, learning_rate
            )
        if train_crew:
            crew_optimizer = OptimizerType.build(
                optimizer_type, crew_model, learning_rate
            )

    # initializing trainer
    trainer = DQNTeamTrainer(
        imposter_optimizer=imposter_optimizer,
        crew_optimizer=crew_optimizer,
        gamma=gamma,
    )

    # initialize scheduler
    scheduler = ExponentialSchedule(
        scheduler_start_eps, scheduler_end_eps, scheduler_time_steps
    )

    # initialize metric handlers
    metrics = EpisodicMetricHandler()

    # initialize replay buffer and prepopulate it
    replay_buffer = ReplayBuffer(
        max_size=replay_buffer_size,
        trajectory_size=sequence_length,
        state_size=env.flattened_state_size,
        n_imposters=env.n_imposters,
        n_agents=env.n_agents,
    )

    replay_buffer.populate(env=env, num_steps=replay_prepopulate_steps)

    # run actual experiment
    train(
        env=env,
        metrics=metrics,
        num_steps=num_steps,
        replay_buffer=replay_buffer,
        featurizer=featurizer,
        imposter_model=imposter_model,
        crew_model=crew_model,
        save_directory_path=experiment_dir,
        train_step_interval=train_step_interval,
        batch_size=batch_size,
        gamma=gamma,
        scheduler=scheduler,
        trainer=trainer,
        num_saves=num_checkpoint_saves,
        target_update_interval=target_update_interval,
    )

    avg_metrics = metrics.compute()

    print(f"Average Metrics: {avg_metrics}")

    # run experiment
    metrics.save_metrics(save_file_path=experiment_dir / "metrics.json")

    return metrics


def train(
    env: FourRoomEnv,
    metrics: EpisodicMetricHandler,
    num_steps: int,
    replay_buffer: ReplayBuffer,
    featurizer: SequenceStateFeaturizer,
    imposter_model: Q_Estimator,
    crew_model: Q_Estimator,
    scheduler: ExponentialSchedule,
    save_directory_path: pathlib.Path,
    trainer: DQNTeamTrainer,
    train_step_interval: int = 5,
    batch_size: int = 32,
    gamma: float = 0.99,
    num_saves: int = 5,
    target_update_interval: int = 10_000,
):
    returns = []
    game_lengths = []
    losses = []
    rewards = []

    imposter_target_model = imposter_model.create_copy()
    crew_target_model = crew_model.create_copy()

    # Initialize structures to store the models at different stages of training
    t_saves = np.linspace(0, num_steps, num_saves - 1, endpoint=False, dtype=int)
    print(f"Saving models at steps: {t_saves}")

    i_episode = 0  # Use this to indicate the index of the current episode
    t_episode = 0  # Use this to indicate the time-step inside current episode

    state, info = env.reset()  # Initialize state of first episode

    state_sequence = np.zeros((replay_buffer.trajectory_size, replay_buffer.state_size))
    for i in range(replay_buffer.trajectory_size):
        state_sequence[i] = env.flatten_state(
            state
        )  # Initialize sequence with current state

    G = np.zeros(env.n_agents)

    # Iterate for a total of `num_steps` steps
    pbar = tqdm.trange(num_steps)
    for t_total in pbar:

        # Save model
        if t_total in t_saves and trainer.train:
            percent_progress = f"{int(t_total * 100 / num_steps)}"
            imposter_model.dump_to_checkpoint(
                save_directory_path / f"imposter_{imposter_model.model_type}_{percent_progress}.pt"
            )
            crew_model.dump_to_checkpoint(
                save_directory_path / f"crew_{crew_model.model_type}_{percent_progress}.pt"
            )

        # Update Target DQNs
        if t_total % target_update_interval == 0:
            imposter_target_model.load_state_dict(imposter_model.state_dict())
            crew_target_model.load_state_dict(crew_model.state_dict())

        # featurizing current trajectory
        featurizer.fit(
            torch.tensor(state_sequence).unsqueeze(0)
        )  # add batch dimension to state_sequence (features expect a batch dimension)

        # getting next action
        eps = scheduler.value(t_total)
        agent_actions = np.zeros(env.n_agents, dtype=np.int32)
        alive_agents = state[env.state_fields[StateFields.ALIVE_AGENTS]]

        with torch.no_grad():
            for agent_idx, (spatial, non_spatial) in enumerate(
                featurizer.generate_featurized_states()
            ):

                # choose action for alive imposter
                if env.imposter_mask[agent_idx] and alive_agents[agent_idx]:

                    if np.random.random() <= eps:
                        agent_actions[agent_idx] = np.random.randint(
                            0, env.n_imposter_actions
                        )
                    else:
                        agent_actions[agent_idx] = int(
                            torch.argmax(imposter_model(spatial, non_spatial))
                        )

                # choose action for alive crew member
                elif alive_agents[agent_idx]:
                    if np.random.random() <= eps:
                        agent_actions[agent_idx] = np.random.randint(
                            0, env.n_crew_actions
                        )
                    else:
                        agent_actions[agent_idx] = int(
                            torch.argmax(crew_model(spatial, non_spatial))
                        )

        next_state, reward, done, trunc, info = env.step(agent_actions=agent_actions)

        rewards.append(reward)
        G = reward + gamma * G

        next_state_sequence = np.roll(state_sequence.copy(), -1, axis=0)
        next_state_sequence[-1] = env.flatten_state(next_state)

        # adding the timestep to replay buffer
        replay_buffer.add(
            state=state_sequence,
            action=agent_actions,
            reward=reward,
            done=done,
            next_state=next_state_sequence,
            imposters=env.imposter_idxs,
        )

        # Training update for imposters and/or crew
        if t_total % train_step_interval == 0:

            # get sample of trajectories to train on
            batch = replay_buffer.sample(batch_size)

            step_losses = trainer.train_step(
                batch=batch,
                featurizer=featurizer,
                imposter_model=imposter_model,
                imposter_target_model=imposter_target_model,
                crew_model=crew_model,
                crew_target_model=crew_target_model,
            )

            losses.append(step_losses)

        # checking if the env needs to be reset
        if done or trunc:

            imposter_return = G[env.imposter_mask].mean().item()
            crew_return = G[~env.imposter_mask].mean().item()

            returns.append([imposter_return, crew_return])

            # update metrics
            metrics.step(info)

            pbar.set_description(
                f"Episode: {i_episode} | Steps: {t_episode + 1} | Epsilon: {eps:4.2f} | Imposter Loss: {losses[-1][0]:4.2f} | Crew Loss: {losses[-1][1]:4.2f} | Imposter Return: {imposter_return:4.2f} | Crew Return: {crew_return:4.2f}"
            )
            
            # resetting episode
            rewards = []
            game_lengths.append(t_episode)
            G = np.zeros(env.n_agents)
            t_episode = 0
            i_episode += 1

            state, _ = env.reset()
            state_sequence = np.zeros(
                (replay_buffer.trajectory_size, replay_buffer.state_size)
            )
            for i in range(replay_buffer.trajectory_size):
                state_sequence[i] = env.flatten_state(state)

        else:
            state = next_state
            state_sequence = next_state_sequence
            t_episode += 1

    # saving final model states
    imposter_model.dump_to_checkpoint(
        save_directory_path / f"imposter_{imposter_model.model_type}_100%.pt"
    )
    crew_model.dump_to_checkpoint(
        save_directory_path / f"crew_{crew_model.model_type}_100%.pt"
    )

    returns = np.array(returns)
    metrics.set({
        SusMetrics.AVG_IMPOSTER_RETURNS: returns[:, 0].tolist(),
        SusMetrics.AVG_CREW_RETURNS: returns[:, 1].tolist(),
    })

    losses = np.array(losses)

    metrics.set({
        SusMetrics.IMPOSTER_LOSS: losses[:, 0].tolist(),
        SusMetrics.CREW_LOSS: losses[:, 1].tolist(),
    })


