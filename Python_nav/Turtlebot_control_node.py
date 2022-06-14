
import os
import select
import sys
import rclpy
from rclpy.node import Node
from nav_msgs.msg import Odometry
from std_msgs.msg import String
from sensor_msgs.msg import JointState
from geometry_msgs.msg import Twist, PoseStamped
from utils.msg import Goal
from rclpy.qos import QoSProfile
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSHistoryPolicy
import numpy as np
import math
# -> Create turtlbot namespace
if len(sys.argv) == 1:
	turtle_id = "1"
else:
	turtle_id = sys.argv[1]
turtle_namespace = "/Turtle_" + turtle_id

# TODO: -> ???
if os.name == 'nt':
    import msvcrt
else:
    import termios
    import tty

# -> Setup turtlebot reference properties
BURGER_MAX_LIN_VEL = 0.22
BURGER_MAX_ANG_VEL = 2.84

WAFFLE_MAX_LIN_VEL = 0.26
WAFFLE_MAX_ANG_VEL = 1.82

LIN_VEL_STEP_SIZE = 0.01
ANG_VEL_STEP_SIZE = 0.1

# -> Fetch turtlebot type from environment
TURTLEBOT3_MODEL = os.environ['TURTLEBOT3_MODEL']

# ================================================================================= Main
class Minimal_path_sequence(Node):
    def __init__(self):
        super().__init__('Turtlebot_1_controller')

        self.verbose = 0

        # -> Setup robot ID
        self.robot_id = "Turtlebot_1"

        # -> Setup goto specs
        self.success_distance_range = .10 
        self.success_angle_range = 7.    # %

        # -> Setup robot states
        self.goal_sequence_backlog = {}
        self.goal_sequence = None
        self.goal_sequence_priority = 0

        self.current_angular_velocity = 0.
        self.current_linear_velocity = 0.

        # -> Create storage variables
        self.position = None
        self.orientation = None

        # -> Initiate datastreams
        # Instruction publisher
        qos = QoSProfile(depth=10)
        
        self.instruction_publisher = self.create_publisher(
            msg_type=Twist,
            topic=f'/Turtle_1/cmd_vel',
            qos_profile=qos
            )

        timer_period = 0.01  # seconds
        self.timer = self.create_timer(
            timer_period, 
            self.instruction_publisher_callback
            )

        # Goal subscription
        self.goal_subscription = self.create_subscription(
            msg_type=Goal,
            topic="/goals_backlog",
            callback=self.goal_subscriber_callback,
            qos_profile=qos
            )

        # Odom subscription
        qos = QoSProfile(
            reliability=QoSReliabilityPolicy.RMW_QOS_POLICY_RELIABILITY_BEST_EFFORT,
            history=QoSHistoryPolicy.RMW_QOS_POLICY_HISTORY_KEEP_LAST,
            depth=1
            )

        self.odom_subscription = self.create_subscription(
            msg_type=PoseStamped,
            topic="/Turtle_1/pose",
            callback=self.odom_subscriber_callback,
            qos_profile=qos
            )

        # -> Status printer
        timer_period = 1.  # seconds
        self.timer = self.create_timer(
            timer_period, 
            self.state_callback
            )

    def state_callback(self):
        print("\n")
        print(f"--> position: {self.position}")
        print(f"--> orientation: {self.orientation}")
        
        if self.goal_sequence is not None:
            print(f"--> Goal: {self.goal}")
            print(f"--> distance_to_goal: {round(self.distance_to_goal, 3)}")
            angle_diff = self.goal_angle - self.orientation
            print(f"--> Angle difference: {round(angle_diff)} degrees ({round(abs(angle_diff/360)*100, 2)}%)")

    def odom_subscriber_callback(self, msg):
        self.position = [msg.pose.position.x, msg.pose.position.y]
        self.orientation = self.euler_from_quaternion(
            quat=msg.pose.orientation
        )[-1]

    def instruction_publisher_callback(self):
        if self.position is None \
            or self.orientation is None:
            print("!!!!!!!!!!!!!!!!!!!!!!!!! Missing sensor data !!!!!!!!!!!!!!!!!!!!!!!!!")
            return

        elif self.goal_sequence is None:
            # -> Get new goal sequence
            self.get_new_goal_sequence()

            if self.goal_sequence is not None:
                print("=================================================================")
                print(f"     New goal sequence selected: {self.goal_sequence}")
                print(f"     New goal sequence priority: {self.goal_sequence_priority}")
                print(f"     First goal: {self.goal}    (distance: {round(self.distance_to_goal, 3)})")
                print("=================================================================")
            return

        # -> Check goal sequence state
        if self.check_goal_sequence():
            self.goal_sequence = None
            print("++++++++++++++++++++++++++++++++++++ Goal Completed ++++++++++++++++++++++++++++++++++++")
            return

        # -> Check subgoal state, remove subgoal reached
        elif self.check_subgoal_state():
            return

        else:
            # -> Construct message
            twist = Twist()

            linear_velocity, angular_velocity = self.determine_instruction()

            # -> Set linear velocity
            twist.linear.x = linear_velocity
            twist.linear.y = 0.0
            twist.linear.z = 0.0

            # -> Set angular velocity
            twist.angular.x = 0.0
            twist.angular.y = 0.0
            twist.angular.z = angular_velocity

            # -> Publish instruction msg to robot
            self.instruction_publisher.publish(msg=twist)

            # -> Publish msg to console (ROS print)
            if self.verbose in [2, 3]:
                self.get_logger().info(f"Publishing: " +
                                    f"\n       x: {twist.linear.x}" +
                                    f"\n       y: {twist.linear.y}" +
                                    f"\n       z: {twist.linear.z}" 
                                    f"\n       ___________"
                                    f"\n       u: {twist.angular.x}" +
                                    f"\n       v: {twist.angular.y}" +
                                    f"\n       w: {twist.angular.z}")
        
    def goal_subscriber_callback(self, msg):
        print(f"++++++++++++++++++++++++++++++ Goal sequence {msg.goal_sequence_id} received by {self.robot_id} ++++++++++++++++++++++++++++++")
        # -> If message is addressed to robot
        if msg.robot_id == self.robot_id:
            goal_sequence = {
                "ID":msg.goal_sequence_id,
                "sequence": []
                }

            # -> Populate sequence
            for point in msg.sequence:
                goal_sequence["sequence"].append([point.x, point.y, point.z])

            # -> Log task to goal_backlog according to priority
            if int(msg.priority) not in self.goal_sequence_backlog.keys():
                self.goal_sequence_backlog[int(msg.priority)] = []

            self.goal_sequence_backlog[int(msg.priority)].append(goal_sequence)
            
    @property
    def path_vector(self) -> list:
        path_vector = [
            self.goal[0] - self.position[0],
            self.goal[1] - self.position[1],
        ]

        return path_vector

    @property
    def distance_to_goal(self):
        return math.sqrt(self.path_vector[0]**2 + self.path_vector[1]**2)

    @property
    def goal(self):
        return self.goal_sequence[0]

    @property
    def goal_angle(self):
        return math.atan2(self.path_vector[1], self.path_vector[0]) * 180/math.pi

    def check_subgoal_state(self):
        # -> Remove sub-goal if reached
        if self.distance_to_goal < self.success_distance_range:
            print(f"-------------------------------------> Subgoal {self.goal_sequence[0]} completed")
            self.goal_sequence.pop(0)

            if len(self.goal_sequence) != 0:
                print(f"                                       New subgoal: {self.goal_sequence[0]}   (distance: {round(self.distance_to_goal, 3)})")
            
            print(f"                                       Goal sequence left: {self.goal_sequence}")
            return True

        else:
            return False

    def check_goal_sequence(self):
        return len(self.goal_sequence) == 0

    def get_new_goal_sequence(self):
        # -> Determine highest priority sequence
        selected_goal_sequence_priority = -1

        for goal_priority, goal_sequence_lst in self.goal_sequence_backlog.items():
            if len(goal_sequence_lst) != 0 and goal_priority > selected_goal_sequence_priority:
                selected_goal_sequence_priority = goal_priority

        # -> Retrieve cooresponding goal sequence from goal_backlog
        if selected_goal_sequence_priority != -1:
            goal_sequence = self.goal_sequence_backlog[selected_goal_sequence_priority].pop(0)

            self.goal_sequence = goal_sequence["sequence"]
            self.goal_id = goal_sequence["ID"]
            self.goal_sequence_priority = selected_goal_sequence_priority

    def determine_instruction(self):
        # -> Check whether the robot is aligned with the path vector
        goal_angle = self.goal_angle

        # if goal_angle < 0:
        #     goal_angle = abs(goal_angle)

        # # -> Check for goal quadrant
        # if self.goal[0] > self.position[0]:
        #     goal_angle += 180

        angle_diff = goal_angle - self.orientation
        angle_diff_percent = abs(angle_diff/360)

        if self.verbose == 3:
            print("\n------------------------------------------------------")
            print("self.position:", self.position, "self.goal", self.goal)
            print("self.orientation:", self.orientation)
            print("self.path_vector:", self.path_vector)
            print("self.distance_to_goal:", self.distance_to_goal)
            print("goal_angle: ", goal_angle)        
            print("angle_diff: ", angle_diff)
            print("angle_diff_precent", angle_diff_percent * 100, "%")
            print("success_angle_range", self.success_angle_range)
            print("------------------------------------------------------")
            
        # ======================================================================== Solving for angular velocity
        if abs(angle_diff_percent)*100 > self.success_angle_range:
            if self.verbose in [1, 2, 3]:
                print("--> Correcting angle")
                
            # -> halt robot
            linear_velocity = self.current_linear_velocity * -1
            self.current_linear_velocity = 0.0

            # -> Solve for angular velocity instruction magnitude
            angular_velocity = (BURGER_MAX_ANG_VEL - 1/2*(1-abs(angle_diff_percent)))

            if abs(angular_velocity) > BURGER_MAX_ANG_VEL:
                if angular_velocity < 0:
                    angular_velocity = -BURGER_MAX_ANG_VEL
                else:
                    angular_velocity = BURGER_MAX_ANG_VEL

            # -> Inverse rotation direction if shorter
            if angle_diff > 180 or angle_diff < 0:
                angular_velocity = angular_velocity * -1

            # -> Update current_angular_velocity state
            self.current_angular_velocity = angular_velocity

            return linear_velocity, angular_velocity

        else:
            # -> Halt robot rotation
            angular_velocity = self.current_angular_velocity * -1
            self.current_angular_velocity = 0.0

        # ======================================================================== Solving for linear velocity
        if self.distance_to_goal > self.success_distance_range and self.current_angular_velocity == 0:
            if self.verbose in [1, 2, 3]:
                print("--> Correction velocity")
            # -> Solve for linear velocity instruction
            linear_velocity = (BURGER_MAX_LIN_VEL) - 0.01

            # -> Update current_linear_velocity state
            self.current_linear_velocity = linear_velocity

        else:
            # -> Halt robot
            linear_velocity = self.current_linear_velocity * -1
            self.current_linear_velocity = 0.0

        return linear_velocity, angular_velocity


    @staticmethod
    def euler_from_quaternion(quat):  
        """  
        Convert quaternion (w in last place) to euler roll, pitch, yaw (rad).  
        quat = [x, y, z, w]    
        
        """    
        x = quat.x  
        y = quat.y  
        z = quat.z  
        w = quat.w  

        sinr_cosp = 2 * (w * x + y * z)  
        cosr_cosp = 1 - 2 * (x * x + y * y)  
        roll = np.arctan2(sinr_cosp, cosr_cosp) * 180/math.pi
    
        sinp = 2 * (w * y - z * x)  
        pitch = np.arcsin(sinp) * 180/math.pi
    
        siny_cosp = 2 * (w * z + x * y)  
        cosy_cosp = 1 - 2 * (y * y + z * z)  
        yaw = np.arctan2(siny_cosp, cosy_cosp) * 180/math.pi

        # if yaw < 0:
        #     yaw = (180 - abs(yaw)) + 180
    
        return [roll, pitch, yaw]

def main(args=None):
    # `rclpy` library is initialized
    rclpy.init(args=args)

    path_sequence = Minimal_path_sequence()

    rclpy.spin(path_sequence)

    path_sequence.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
