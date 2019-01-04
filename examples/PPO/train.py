#   Copyright (c) 2018 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import argparse
import gym
import numpy as np
from mujoco_agent import MujocoAgent
from mujoco_model import MujocoModel
from parl.algorithms import PPO
from parl.utils import logger, action_mapping
from utils import *


def run_train_episode(env, agent, scaler):
    obs = env.reset()
    observes, actions, rewards, unscaled_obs = [], [], [], []
    done = False
    step = 0.0
    scale, offset = scaler.get()
    scale[-1] = 1.0  # don't scale time step feature
    offset[-1] = 0.0  # don't offset time step feature
    while not done:
        obs = obs.reshape((1, -1))
        obs = np.append(obs, [[step]], axis=1)  # add time step feature
        unscaled_obs.append(obs)
        obs = (obs - offset) * scale  # center and scale observations
        obs = obs.astype('float32')
        observes.append(obs)

        action = agent.policy_sample(obs)
        action = np.clip(action, -1.0, 1.0)
        action = action_mapping(action, env.action_space.low[0],
                                env.action_space.high[0])

        action = action.reshape((1, -1)).astype('float32')
        actions.append(action)

        obs, reward, done, _ = env.step(np.squeeze(action))
        rewards.append(reward)
        step += 1e-3  # increment time step feature

    return (np.concatenate(observes), np.concatenate(actions),
            np.array(rewards, dtype='float32'), np.concatenate(unscaled_obs))


def run_evaluate_episode(env, agent, scaler):
    obs = env.reset()
    rewards = []
    step = 0.0
    scale, offset = scaler.get()
    scale[-1] = 1.0  # don't scale time step feature
    offset[-1] = 0.0  # don't offset time step feature
    while True:
        obs = obs.reshape((1, -1))
        obs = np.append(obs, [[step]], axis=1)  # add time step feature
        obs = (obs - offset) * scale  # center and scale observations
        obs = obs.astype('float32')

        action = agent.policy_predict(obs)
        action = action_mapping(action, env.action_space.low[0],
                                env.action_space.high[0])

        obs, reward, done, _ = env.step(np.squeeze(action))
        rewards.append(reward)

        step += 1e-3  # increment time step feature
        if done:
            break
    return np.sum(rewards)


def collect_trajectories(env, agent, scaler, episodes):
    all_obs, all_actions, all_rewards, all_unscaled_obs = [], [], [], []
    for e in range(episodes):
        obs, actions, rewards, unscaled_obs = run_train_episode(
            env, agent, scaler)
        all_obs.append(obs)
        all_actions.append(actions)
        all_rewards.append(rewards)
        all_unscaled_obs.append(unscaled_obs)
    scaler.update(np.concatenate(all_unscaled_obs)
                  )  # update running statistics for scaling observations
    return np.concatenate(all_obs), np.concatenate(
        all_actions), np.concatenate(all_rewards)


def main():
    env = gym.make(args.env)

    obs_dim = env.observation_space.shape[0]
    act_dim = env.action_space.shape[0]
    obs_dim += 1  # add 1 to obs dim for time step feature

    scaler = Scaler(obs_dim)

    model = MujocoModel(obs_dim, act_dim)
    hyperparas = {
        'act_dim': act_dim,
        'policy_lr': model.policy_lr,
        'value_lr': model.value_lr
    }
    alg = PPO(model, hyperparas)
    agent = MujocoAgent(
        alg, obs_dim, act_dim, args.kl_targ, loss_type=args.loss_type)

    # run a few episodes to initialize scaler
    collect_trajectories(env, agent, scaler, episodes=5)

    episode = 0
    while episode < args.num_episodes:
        obs, actions, rewards = collect_trajectories(
            env, agent, scaler, episodes=args.episodes_per_batch)
        episode += args.episodes_per_batch

        pred_values = agent.value_predict(obs)

        # scale rewards
        scale_rewards = rewards * (1 - args.gamma)

        discount_sum_rewards = calc_discount_sum_rewards(
            scale_rewards, args.gamma)
        discount_sum_rewards = discount_sum_rewards.astype('float32')

        advantages = calc_gae(scale_rewards, pred_values, args.gamma, args.lam)
        # normalize advantages
        advantages = (advantages - advantages.mean()) / (
            advantages.std() + 1e-6)
        advantages = advantages.astype('float32')

        policy_loss, kl = agent.policy_learn(obs, actions, advantages)
        value_loss = agent.value_learn(obs, discount_sum_rewards)

        logger.info(
            'Episode {}, Train reward: {}, Policy loss: {}, KL: {}, Value loss: {}'
            .format(episode,
                    np.sum(rewards) / args.episodes_per_batch, policy_loss, kl,
                    value_loss))
        if episode % (args.episodes_per_batch * 5) == 0:
            eval_reward = run_evaluate_episode(env, agent, scaler)
            logger.info('Episode {}, Evaluate reward: {}'.format(
                episode, eval_reward))


if __name__ == "__main__":

    parser = argparse.ArgumentParser()
    parser.add_argument(
        '--env',
        type=str,
        help='Mujoco environment name',
        default='HalfCheetah-v2')
    parser.add_argument(
        '--num_episodes',
        type=int,
        help='Number of episodes to run',
        default=10000)
    parser.add_argument(
        '--gamma', type=float, help='Discount factor', default=0.995)
    parser.add_argument(
        '--lam',
        type=float,
        help='Lambda for Generalized Advantage Estimation',
        default=0.98)
    parser.add_argument(
        '--kl_targ', type=float, help='D_KL target value', default=0.003)
    parser.add_argument(
        '--episodes_per_batch',
        type=int,
        help='Number of episodes per training batch',
        default=5)
    parser.add_argument(
        '--loss_type',
        type=str,
        help="Choose loss type of PPO algorithm, 'CLIP' or 'KLPEN'",
        default='CLIP')

    args = parser.parse_args()
    import time
    logger.set_dir('./log_dir/{}_{}'.format(args.loss_type, time.time()))
    main()