from typing import Tuple

import numpy as np
import yaml
import os

import env.transform_utils as T
from env.furniture_sawyer import FurnitureSawyerEnv
from env.models import furniture_name2id
from util import PrettySafeLoader
from util.logger import logger


class FurnitureSawyerDenseRewardEnv(FurnitureSawyerEnv):
    """
    Sawyer environment.
    """

    def __init__(self, config):
        """
        Args:
            config: configurations for the environment.
        """
        # config.furniture_name = "table_lack_0825"
        config.furniture_id = furniture_name2id[config.furniture_name]
        config.object_ob_all = False
        super().__init__(config)
        # default values for rew function
        self._env_config.update(
            {
                "pos_dist": 0.015,
                "rot_dist_up": 0.95,
                "rot_dist_forward": 0.9,
                "project_dist": -1,
            }
        )
        self._diff_rew = config.diff_rew
        self._phase_bonus = config.phase_bonus
        self._ctrl_penalty_coef = config.ctrl_penalty_coef
        self._pos_threshold = config.pos_threshold
        self._rot_threshold = config.rot_threshold
        self._rot_dist_coef = config.rot_dist_coef
        self._pos_dist_coef = config.pos_dist_coef
        self._gripper_penalty_coef = config.gripper_penalty_coef
        self._align_rot_dist_coef = config.align_rot_dist_coef
        self._fine_align_rot_dist_coef = config.fine_align_rot_dist_coef
        self._fine_pos_dist_coef = config.fine_pos_dist_coef
        self._touch_coef = config.touch_coef

        self._num_connect_steps = 0
        self._discrete_grip = config.discrete_grip
        self._grip_open_phases = set(["move_eef_above_leg", "lower_eef_to_leg"])
        self._phases = ["move_eef_above_leg", "lower_eef_to_leg", "grasp_leg"]
        self._phases.extend(["move_leg", "move_leg_fine"])
        # Load the furniture recipe
        fullpath = os.path.join(
            os.path.dirname(__file__), f"../demos/recipes/{config.furniture_name}.yaml"
        )
        with open(fullpath, "r") as stream:
            self._data = data = yaml.load(stream, Loader=PrettySafeLoader)
            self._recipe = data["recipe"]
            self._site_recipe = data["site_recipe"]
            part = self._recipe[0][0]
            g1, g2 = f"{part}_ltgt_site0", f"{part}_rtgt_site0"
            if "grip_site_recipe" in data:
                g1, g2 = data["grip_site_recipe"][0]
            self._get_leg_grasp_pos = (
                lambda x: (self._get_pos(g1) + self._get_pos(g2)) / 2
            )
            self._get_leg_grasp_vector = lambda x: self._get_pos(g1) - self._get_pos(g2)

    def _reset_reward_variables(self):
        self._subtask_step = 0
        self._update_reward_variables(self._subtask_step)

    def _set_next_subtask(self) -> bool:
        """Returns True if we are done with all attaching steps"""
        self._subtask_step += 1
        if self._subtask_step == len(self._site_recipe):
            return True
        self._update_reward_variables(self._subtask_step)
        return False

    def _update_reward_variables(self, subtask_step):
        """Update the reward variables wrt subtask step"""
        self._phase_i = 0
        self._leg, self._table = self._recipe[subtask_step]
        self._leg_site, self._table_site = self._site_recipe[subtask_step]
        # updates the observation to the current objects of interest
        self._subtask_part1 = self._object_name2id[self._leg]
        self._subtask_part2 = self._object_name2id[self._table]
        self._touched = False
        self._leg_lift = False
        self._init_leg_pos = self._get_pos(self._leg)
        self._leg_fine_aligned = False

        if self._diff_rew:
            eef_pos = self._get_pos("griptip_site")
            leg_pos = self._init_leg_pos + [0, 0, 0.05]
            xy_distance = np.linalg.norm(eef_pos[:2] - leg_pos[:2])
            z_distance = np.abs(eef_pos[2] - leg_pos[2])
            self._prev_eef_above_leg_distance = xy_distance + z_distance

        if subtask_step == 0:  # don't need to update getters
            return
        self._recipe = self._data["recipe"]
        self._site_recipe = self._data["site_recipe"]
        g1, g2 = f"{self._leg}_ltgt_site0", f"{self._leg}_rtgt_site0"
        if "grip_site_recipe" in self._data and self._subtask_step < len(
            self._data["grip_site_recipe"]
        ):
            g1, g2 = self._data["grip_site_recipe"][self._subtask_step]
        self._get_leg_grasp_pos = lambda x: (self._get_pos(g1) + self._get_pos(g2)) / 2
        self._get_leg_grasp_vector = lambda x: self._get_pos(g1) - self._get_pos(g2)

    def _reset(self, furniture_id=None, background=None):
        super()._reset(furniture_id, background)
        self._reset_reward_variables()

    def _step(self, a):
        """
        Takes a simulation step with @a and computes reward.
        """
        # discretize gripper action
        if self._discrete_grip:
            a = a.copy()
            a[-2] = -1 if a[-2] < 0 else 1

        ob, _, done, _ = super(FurnitureSawyerEnv, self)._step(a)
        reward, done, info = self._compute_reward(a)
        return ob, reward, done, info

    def _compute_reward(self, ac) -> Tuple[float, bool, dict]:
        """
        Multistage reward.
        While moving the leg, we need to make sure the grip is stable by measuring
        angular movements.
        At any point, the robot should minimize pose displacement in non-relevant parts.

        Phases:
        move_eef_over_leg: move eef over table leg
        lower_eef_to_leg: lower eef onto the leg
        lift_leg: grip and lift the leg
        move_eef_over_conn: move the eef (holding leg) above the conn site
        align_leg: coarsely align the leg with the conn site
        lower_leg: move the leg 0.05 cm above the conn site
        align_leg_fine: fine grain alignment of the up and forward vectors
        lower_leg_fine: finely move the leg onto the conn site
        """
        phase_bonus = reward = 0
        done = False
        info = {"subtask": self._subtask_step}
        phase = self._phases[self._phase_i]

        ctrl_penalty, ctrl_info = self._ctrl_penalty(ac)
        stable_grip_rew, sg_info = self._stable_grip_reward()
        grip_penalty, grip_info = self._gripper_penalty(ac)

        # detect early success
        info["is_aligned"] = int(self._is_aligned(self._leg_site, self._table_site))
        left, right = self._finger_contact(self._leg)
        if phase != "move_leg_fine" and info["is_aligned"] and left and right:
            phase_info = {}
            phase_reward = 300
            phase_info["connect_rew"] = ac[-1] * 300
            reward += phase_info["connect_rew"]
            phase_info["connect_succ"] = int(info["is_aligned"] and ac[-1] > 0)
            if phase_info["connect_succ"]:
                phase_reward = 20000
                self._phase_i = 5
                print(f"Early Connected!!!")
                # update reward variables for next attachment
                done = self._success = self._set_next_subtask()
        elif phase == "move_eef_above_leg":
            phase_reward, phase_info = self._move_eef_above_leg_reward()
            if phase_info[f"{phase}_succ"] and sg_info["stable_grip_succ"]:
                print(f"DONE WITH PHASE {phase}")
                self._phase_i += 1
                phase_bonus = 5
                eef_pos = self._get_gripper_pos()
                leg_pos1 = self._get_pos(self._leg) + [0, 0, -0.015]
                leg_pos2 = leg_pos1 + [0, 0, 0.03]
                leg_pos = np.concatenate([leg_pos1, leg_pos2])
                xy_distance = np.linalg.norm(eef_pos[:2] - leg_pos[:2])
                z_distance = np.abs(eef_pos[2] - leg_pos[2])
                self._prev_eef_leg_distance = xy_distance + z_distance
        elif phase == "lower_eef_to_leg":
            phase_reward, phase_info = self._lower_eef_to_leg_reward()
            if phase_info[f"{phase}_succ"] and sg_info["stable_grip_succ"]:
                print(f"DONE WITH PHASE {phase}")
                phase_bonus = 50
                self._phase_i += 1
                self._prev_grasp_leg_rew = ac[-2]
        elif phase == "grasp_leg":
            phase_reward, phase_info = self._grasp_leg_reward(ac)
            if phase_info[f"grasp_leg_succ"]:
                print(f"DONE WITH PHASE {phase}")
                self._phase_i += 1
                phase_bonus = self._phase_bonus
                above_table_site = self._get_pos(self._table_site)
                above_table_site[2] += 0.05
                leg_site = self._get_pos(self._leg_site)
                self._prev_move_pos_distance = np.linalg.norm(
                    above_table_site - leg_site
                )
                leg_up = self._get_up_vector(self._leg_site)
                table_up = self._get_up_vector(self._table_site)
                self._prev_move_ang_dist = T.cos_siml(leg_up, table_up)
        elif phase == "move_leg":
            phase_reward, phase_info = self._move_leg_reward()
            if not phase_info["touch"]:
                print("Dropped leg")
                phase_bonus = -100
                done = True
            if phase_info[f"{phase}_succ"]:
                print(f"DONE WITH PHASE {phase}")
                self._phase_i += 1
                phase_bonus = self._phase_bonus * 4
                table_site = self._get_pos(self._table_site)
                leg_site = self._get_pos(self._leg_site)
                self._prev_move_pos_distance = np.linalg.norm(table_site - leg_site)

                leg_up = self._get_up_vector(self._leg_site)
                table_up = self._get_up_vector(self._table_site)
                self._prev_move_ang_dist = T.cos_siml(leg_up, table_up)
        elif phase == "move_leg_fine":
            phase_reward, phase_info = self._move_leg_fine_reward(ac)
            if not phase_info["touch"]:
                print("Dropped leg")
                phase_bonus = -125
                done = True
            if phase_info["connect_succ"]:
                phase_bonus = 20000
                self._phase_i += 1
                print(f"CONNECTED!!!!!!!!!!!!!!!!!!!!!!")
                # update reward variables for next attachment
                done = self._success = self._set_next_subtask()
        else:
            phase_reward, phase_info = 0, {}
            done = True

        reward += ctrl_penalty + phase_reward + stable_grip_rew
        reward += grip_penalty + phase_bonus
        info["phase_bonus"] = phase_bonus
        info = {**info, **ctrl_info, **phase_info, **sg_info, **grip_info}
        # log phase if last frame
        if self._episode_length == self._env_config["max_episode_steps"] - 1 or done:
            info["phase"] = self._phase_i
        return reward, done, info

    def _move_eef_above_leg_reward(self) -> Tuple[float, dict]:
        """
        Moves the eef above the leg and rotates the wrist.
        Negative euclidean distance between eef xy and leg xy.

        Return negative eucl distance
        """
        eef_pos = self._get_pos("griptip_site")
        leg_pos = self._get_leg_grasp_pos(self._leg) + [0, 0, 0.05]
        xy_distance = np.linalg.norm(eef_pos[:2] - leg_pos[:2])
        z_distance = np.abs(eef_pos[2] - leg_pos[2])
        eef_above_leg_distance = xy_distance + z_distance
        if self._diff_rew:
            offset = self._prev_eef_above_leg_distance - eef_above_leg_distance
            rew = offset * self._pos_dist_coef
            self._prev_eef_above_leg_distance = eef_above_leg_distance
        else:
            rew = -eef_above_leg_distance * self._pos_dist_coef
        info = {"eef_above_leg_dist": eef_above_leg_distance, "eef_above_leg_rew": rew}
        info["move_eef_above_leg_succ"] = int(xy_distance < 0.015 and z_distance < 0.02)
        # print("-" * 80)
        # print(eef_pos, leg_pos, eef_above_leg_distance)
        return rew, info

    def _lower_eef_to_leg_reward(self) -> Tuple[float, dict]:
        """
        Moves the eef over the leg and rotates the wrist.
        Negative euclidean distance between eef xy and leg xy.
        Give additional reward for contacting the leg
        Return negative eucl distance
        """
        info = {}
        eef_pos = self._get_gripper_pos()
        leg_pos = self._get_leg_grasp_pos(self._leg) + [0, 0, -0.015]
        xy_distance = np.linalg.norm(eef_pos[:2] - leg_pos[:2])
        z_distance = np.abs(eef_pos[2] - leg_pos[2])
        eef_leg_distance = xy_distance + z_distance
        if self._diff_rew:
            offset = self._prev_eef_leg_distance - eef_leg_distance
            rew = offset * self._pos_dist_coef
            self._prev_eef_leg_distance = eef_leg_distance
        else:
            rew = -eef_leg_distance * self._pos_dist_coef
        info.update({"eef_leg_dist": eef_leg_distance, "eef_leg_rew": rew})
        info["lower_eef_to_leg_succ"] = int(xy_distance < 0.015 and z_distance < 0.01)
        return rew, info

    def _grasp_leg_reward(self, ac) -> Tuple[float, dict]:
        """
        Grasp the leg, making sure it is in position
        """
        rew, info = self._lower_eef_to_leg_reward()
        # if eef in correct position, add additional grasping success
        info.update({"grasp_leg_succ": 0, "grasp_leg_rew": 0})

        left, right = self._finger_contact(self._leg)
        leg_touched = int(left and right)
        info["touch"] = leg_touched
        grasp = ac[-2] > 0.5
        info["grasp_leg_succ"] = int(leg_touched and grasp)
        # closed gripper is 1, want to maximize gripper
        offset = ac[-2] - self._prev_grasp_leg_rew
        grasp_leg_rew = offset * self._gripper_penalty_coef * 40
        self._prev_grasp_leg_rew = ac[-2]
        info["grasp_leg_rew"] = grasp_leg_rew

        touch_rew = (leg_touched - 1) * self._touch_coef
        info.update({"touch": leg_touched, "touch_rew": touch_rew})
        # gripper rew, 1 if closed
        # further bonus for touch
        if leg_touched and not self._touched:
            touch_rew += 10
            self._touched = True
        rew += info["grasp_leg_rew"] + touch_rew

        return rew, info

    def _move_leg_reward(self) -> Tuple[float, dict]:
        """
        Coarsely move the leg site over the connsite
        Also give reward for angular alignment
        """
        left, right = self._finger_contact(self._leg)
        leg_touched = int(left and right)
        touch_rew = (leg_touched - 1) * self._touch_coef
        info = {"touch": leg_touched, "touch_rew": touch_rew}

        # calculate position rew
        above_table_site = self._get_pos(self._table_site) + [0, 0, 0.05]
        leg_site = self._get_pos(self._leg_site)
        move_pos_distance = np.linalg.norm(above_table_site - leg_site)
        if self._diff_rew:
            offset = self._prev_move_pos_distance - move_pos_distance
            pos_rew = offset * self._pos_dist_coef * 10
            self._prev_move_pos_distance = move_pos_distance
        else:
            pos_rew = -move_pos_distance * self._pos_dist_coef
        info.update({"move_pos_dist": move_pos_distance, "move_pos_rew": pos_rew})
        # calculate angular rew
        leg_up = self._get_up_vector(self._leg_site)
        table_up = self._get_up_vector(self._table_site)
        move_ang_dist = T.cos_siml(leg_up, table_up)
        ang_rew = (move_ang_dist - 1) * self._align_rot_dist_coef
        info.update({"move_ang_dist": move_ang_dist, "move_ang_rew": ang_rew})
        info["move_leg_succ"] = int(move_pos_distance < 0.06 and move_ang_dist > 0.85)
        rew = pos_rew + ang_rew
        # give one time reward for lifting the leg
        leg_lift = leg_site[2] > (self._init_leg_pos[2] + 0.002)
        if leg_lift and not self._leg_lift:
            print("lift leg")
            self._leg_lift = True
            rew += 10
        return rew, info

    def _move_leg_fine_reward(self, ac) -> Tuple[float, dict]:
        """
        Finely move the leg site over the connsite
        Also give reward for angular alignment
        Also check for connected pieces
        """
        left, right = self._finger_contact(self._leg)
        leg_touched = int(left and right)
        touch_rew = (leg_touched - 1) * self._touch_coef
        info = {"touch": leg_touched, "touch_rew": touch_rew}

        # calculate position rew
        table_site = self._get_pos(self._table_site)
        leg_site = self._get_pos(self._leg_site)
        xy_distance = np.linalg.norm(table_site[:2] - leg_site[:2])
        z_distance = np.linalg.norm(table_site[2] - leg_site[2])
        move_pos_distance = xy_distance + z_distance

        if self._diff_rew:
            offset = self._prev_move_pos_distance - move_pos_distance
            pos_rew = offset * self._fine_pos_dist_coef * 10
            self._prev_move_pos_distance = move_pos_distance
        else:
            pos_rew = -move_pos_distance * self._fine_pos_dist_coef
        info.update(
            {"move_fine_pos_dist": move_pos_distance, "move_fine_pos_rew": pos_rew}
        )
        # calculate angular rew
        leg_up = self._get_up_vector(self._leg_site)
        table_up = self._get_up_vector(self._table_site)
        move_ang_dist = T.cos_siml(leg_up, table_up)
        ang_rew = (move_ang_dist - 1) * self._fine_align_rot_dist_coef
        info["move_fine_ang_dist"] = move_ang_dist

        # proj will approach -1 if aligned correctly
        proj_t = T.cos_siml(table_up, leg_site - table_site)
        proj_l = T.cos_siml(-leg_up, table_site - leg_site)
        proj_t_rew = (-proj_t - 1) * self._fine_align_rot_dist_coef
        proj_l_rew = (-proj_l - 1) * self._fine_align_rot_dist_coef
        info.update({"proj_t_rew": proj_t_rew, "proj_t": proj_t})
        info.update({"proj_l_rew": proj_l_rew, "proj_l": proj_l})
        ang_rew += proj_t_rew + proj_l_rew
        info["move_leg_fine_succ"] = int(
            self._is_aligned(self._leg_site, self._table_site)
        )
        info["move_fine_ang_rew"] = ang_rew
        rew = pos_rew + ang_rew
        # 1 time? bonus for finely aligning the leg
        if info["move_leg_fine_succ"]:  # and not self._leg_fine_aligned:
            self._leg_fine_aligned = True
            rew += 300
        # add additional reward for connection
        if info["move_leg_fine_succ"]:
            info["connect_rew"] = ac[-1] * 300
            rew += info["connect_rew"]
        info["connect_succ"] = int(info["move_leg_fine_succ"] and ac[-1] > 0)
        return rew, info

    def _stable_grip_reward(self) -> Tuple[float, dict]:
        """
        Makes sure the eef and object axes are aligned
        Prioritize wrist alignment more than vertical alignment
        Returns negative angular distance
        """
        # up vector of leg and world up vector should be aligned
        eef_up = self._get_up_vector("grip_site")
        eef_up_grasp_dist = T.cos_siml(eef_up, [0, 0, -1])
        eef_up_grasp_rew = self._rot_dist_coef / 3 * (eef_up_grasp_dist - 1)

        grasp_vec = self._get_leg_grasp_vector(self._leg_site)
        # up vector of leg and forward vector of grip site should be parallel (close to -1 or 1)
        eef_forward = self._get_forward_vector("grip_site")
        eef_forward_grasp_dist = T.cos_siml(eef_forward[:2], grasp_vec[:2])
        eef_forward_grasp_rew = (
            np.abs(eef_forward_grasp_dist) - 1
        ) * self._rot_dist_coef
        info = {
            "eef_up_grasp_dist": eef_up_grasp_dist,
            "eef_up_grasp_rew": eef_up_grasp_rew,
            "eef_forward_grasp_dist": eef_forward_grasp_dist,
            "eef_forward_grasp_rew": eef_forward_grasp_rew,
        }
        # print(f"Close to 1; eef_up_grasp_siml: {eef_up_grasp_dist}")
        # print(f"Close to 1/-1; eef_forward_grasp_dist: {eef_forward_grasp_dist}")
        rew = eef_up_grasp_rew + eef_forward_grasp_rew
        info["stable_grip_succ"] = int(
            eef_up_grasp_dist > 1 - self._rot_threshold
            and np.abs(eef_forward_grasp_dist) > 1 - self._rot_threshold
        )
        return rew, info

    def _gripper_penalty(self, ac) -> Tuple[float, dict]:
        """
        Give penalty on status of gripper. Only give it on phases where
        gripper should close
        Returns 0 if gripper is in desired position, range is [-2, 0]
        """
        if self._discrete_grip:
            ac = ac.copy()
            ac[-2] = -1 if ac[-2] < 0 else 1
        grip_open = self._phases[self._phase_i] in self._grip_open_phases
        # ac[-2] is -1 for open, 1 for closed
        rew = 0
        if not grip_open:
            rew = (
                -1 - ac[-2] if grip_open else ac[-2] - 1
            ) * self._gripper_penalty_coef
        assert rew <= 0
        info = {"gripper_penalty": rew}
        return rew, info

    def _ctrl_penalty(self, action) -> Tuple[float, dict]:
        rew = np.linalg.norm(action[:-2]) * -self._ctrl_penalty_coef
        info = {"ctrl_penalty": rew}
        assert rew <= 0
        return rew, info

    def _other_parts_penalty(self) -> Tuple[float, dict]:
        """
        At any point, the robot should minimize pose displacement in non-relevant parts.
        Return negative reward
        """
        rew = 0
        info = {"opp_penalty": rew}
        assert rew <= 0
        return rew, info

    def _get_gripper_pos(self) -> list:
        """return 6d pos [griptip, grip] """
        return np.concatenate(
            [self._get_pos("griptip_site"), self._get_pos("grip_site")]
        )

    def _get_fingertip_pos(self) -> list:
        """return 6d pos [left grip, right grip]"""
        return np.concatenate(
            [self._get_pos("lgriptip_site"), self._get_pos("rgriptip_site")]
        )


def main():
    from config import create_parser

    parser = create_parser(env="furniture-sawyer-densereward-v0")
    config, unparsed = parser.parse_known_args()
    if len(unparsed):
        logger.error("Unparsed argument is detected:\n%s", unparsed)
        return

    # create an environment and run manual control of Sawyer environment
    env = FurnitureSawyerDenseRewardEnv(config)
    # for i in range(100):
    #     env.reset()
    #     env.render()
    #     print("resetting", i)
    env.run_manual(config)


if __name__ == "__main__":
    main()
