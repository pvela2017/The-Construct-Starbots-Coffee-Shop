#!/usr/bin/env python3

"""


    Pablo
    2023/12/26
"""

import cv2
import numpy as np
import math
import threading 
import asyncio

import rclpy
from rclpy.node import Node
from cv_bridge import CvBridge
from sensor_msgs.msg import Image, CameraInfo
from geometry_msgs.msg import Point
from hole_detector.srv import HoleCoordinates 

from tf2_ros import TransformException
from tf2_ros.buffer import Buffer
from tf2_ros.transform_listener import TransformListener

# transformation.py was not included in tf2
# https://answers.ros.org/question/368446/how-do-i-use-tf2_ros-to-convert-from-quaternions-to-euler/
# http://matthew-brett.github.io/transforms3d/reference/index.html
# https://github.com/matthew-brett/transforms3d
from transforms3d.quaternions import quat2mat
 


class HoleDetector(Node):

    def __init__(self):
        super().__init__('hole_detector_node')

        # Service callback
        self.srv_ = self.create_service(HoleCoordinates, 'holes_coordinates', self.holes_coordinates_callback)
        # Camera info topic subscriber
        self.camera_info_subscriber_ = self.create_subscription(CameraInfo, '/wrist_rgbd_depth_sensor/depth/camera_info', self.camera_info_callback, 10)
        # Camera image subscriber
        self.image_subscriber_ = self.create_subscription(Image, '/wrist_rgbd_depth_sensor/image_raw', self.image_callback, 10)
        # Camera depth aligned subscriber
        self.image_depth_aligned_subscriber_ = self.create_subscription(Image, '/wrist_rgbd_depth_sensor/depth/image_raw', self.image_depth_aligned_callback, 10)
        # Image publisher just for visualization
        self.image_publisher_ = self.create_publisher(Image, '/detected_holes_image', 10)

        # Create a CvBridge to convert ROS Image messages to OpenCV images
        self.bridge_ = CvBridge()

        self.mutex_ = threading.Lock()

        # Frames to transform
        self.source_frame_ = "wrist_rgbd_camera_depth_optical_frame" # simulation
        self.target_frame_ = "base_link"

        # Parameter Initialization
        self.cv_depth_ = np.array([], dtype=np.float32)
        self.homogeneous_matrix_ = np.array([], dtype=np.float32)
        self.holes_coordinates_ = []
        self.intrinsic_flag_ = False

        # Load extrinsic matrix 
        while not self.tf_calculation():
            pass


    def holes_coordinates_callback(self, request, response):
        """
            Service to get the hole coordinates
        """
        # Check if the coordinate list is empty
        if self.holes_coordinates_:
            response.success = True
            for pt in self.holes_coordinates_:
                # Fill the message
                point = Point()
                point.x = float(pt[0])
                point.y = float(pt[1])
                point.z = float(pt[2])
                response.coordinates.append(point)
        else:
            response.success = False

        return response


    def camera_info_callback(self, msg):
        """
            Get intrinsic parameter from the camera
                Intrinsic camera matrix for the raw (distorted) images.
                    [fx  0 cx]
                K = [ 0 fy cy]
                    [ 0  0  1]
        """
        camera_parameter = np.array(msg.k, dtype=np.float32)
        camera_parameter = camera_parameter.reshape((3, 3))
        self.fx_ = camera_parameter[0, 0]
        self.fy_ = camera_parameter[1, 1]
        self.cx_ = camera_parameter[0, 2]
        self.cy_ = camera_parameter[1, 2]
        self.destroy_subscription(self.camera_info_subscriber_) # After getting the parameters no need to call again
        self.get_logger().info("Intrisic parameters captured")
        self.intrinsic_flag_ = True


    def image_callback(self, msg):
        """
            Detect and extract the holes coordinates (in image coordinates)
            of the cup holder robot, also draw the circles
            in a new image for visualition purposes
        """

        # Check if the extrinsic parameter were already loaded
        if not np.any(self.homogeneous_matrix_):
            self.get_logger().warning("Camera extrinsic parameters not loaded yet")
            return

        # Check if the intrisic parameter were already loaded
        if not self.intrinsic_flag_:
            self.get_logger().warning("Camera intrisic parameters not loaded yet")
            return

        self.cv_image_ = self.bridge_.imgmsg_to_cv2(msg, desired_encoding="bgr8")
        # Save image for cv analisis
        # cv2.imwrite('saved_image.png', self.cv_image_)

        # Change to gray scale 
        gray = cv2.cvtColor(self.cv_image_, cv2.COLOR_BGR2GRAY)

        # Simulation: 104
        # Real rgb: 120
        # Saved: 112
        ret, thresh1 = cv2.threshold(gray, 112, 255, cv2.THRESH_BINARY_INV)

        # Blur using 3 * 3 kernel. 
        blurred = cv2.blur(thresh1, (3, 3)) 
          
        # Apply Hough transform on the blurred image. 
        detected_circles = cv2.HoughCircles(blurred,  
                                            cv2.HOUGH_GRADIENT_ALT, 
                                            1.5, 
                                            10, 
                                            param1 = 300, 
                                            param2 = 0.8, 
                                            minRadius = 20, 
                                            maxRadius = 50)

        detected_base = cv2.HoughCircles(blurred,  
                                         cv2.HOUGH_GRADIENT_ALT, 
                                         1.5, 
                                         10, 
                                         param1 = 300, 
                                         param2 = 0.8, 
                                         minRadius = 100, 
                                         maxRadius = 200)  

        # Thread safe for holes_coordinates variable
        with self.mutex_:
            # Process base first
            success_base_flag = False
            if detected_base is not None:
                # Convert the circle parameters a, b and r to integers. 
                detected_base = np.uint16(np.around(detected_base))
                for pt in detected_base[0, :]: 
                    a_b, b_b, r_b = pt[0], pt[1], pt[2]                     

                    # Save the coordinates
                    coor_base = self.get_world_coord([a_b, b_b])
                    if len(coor_base) < 3:
                        success_base_flag = False
                    else:
                        # Draw the circumference of the base. 
                        cv2.circle(self.cv_image_, (a_b, b_b), r_b, (255, 0, 0), 2)
                        success_base_flag = True

            # Draw circles if detected. 
            if detected_circles is not None and success_base_flag: 
                # Clear the array
                self.holes_coordinates_.clear()
              
                # Convert the circle parameters a, b and r to integers. 
                detected_circles = np.uint16(np.around(detected_circles)) 
              
                for pt in detected_circles[0, :]: 
                    a, b, r = pt[0], pt[1], pt[2]                     

                    # Save the coordinates
                    coor = self.get_world_coord([a, b])
                    if len(coor) < 3:
                        continue

                    # Draw the circumference of the circle. 
                    cv2.circle(self.cv_image_, (a, b), r, (0, 255, 0), 2)               
                    # Draw a small circle (of radius 1) to show the center. 
                    cv2.circle(self.cv_image_, (a, b), 1, (0, 0, 255), 3)

                    # Use the Z component of the base since this is constant
                    # meanwhile the one of the hole will change due to the
                    # camera angle
                    const_z_coor = np.array([coor[0], coor[1], coor_base[2]])

                    self.holes_coordinates_.append(const_z_coor)
                    # Write the coordinates on the image
                    font = cv2.FONT_HERSHEY_SIMPLEX 
                    fontScale = 0.3
                    color = (0, 255, 0)
                    thickness = 1
                    x_string = "x:" + str(float(const_z_coor[0])*10)[:3]
                    y_string = "y:" + str(float(const_z_coor[1])*10)[:3]
                    z_string = "z:" + str(float(const_z_coor[2])*10)[:3]
                    cv2.putText(self.cv_image_, x_string, (a + 25, b - 11), font, fontScale, color, thickness, cv2.LINE_AA)
                    cv2.putText(self.cv_image_, y_string, (a + 25, b), font, fontScale, color, thickness, cv2.LINE_AA)
                    cv2.putText(self.cv_image_, z_string, (a + 25, b + 11), font, fontScale, color, thickness, cv2.LINE_AA)
                    
            else:
               self.holes_coordinates_ = []
            
        # Create and publish the image with the holes drown
        ros_image = self.bridge_.cv2_to_imgmsg(self.cv_image_, encoding="bgr8")
        self.image_publisher_.publish(ros_image)


    def image_depth_aligned_callback(self, msg):
        self.cv_depth_ = self.bridge_.imgmsg_to_cv2(msg, desired_encoding="passthrough")


    def tf_calculation(self):
        """
            Get the transform from the camera frame
            to the robot base
        """
        tf_buffer = Buffer()
        tf_listener = TransformListener(tf_buffer, self)


        # Wait for the tf without this there is error
        # https://answers.ros.org/question/387616/ros2-foxy-lookuptransform-target-doesnt-exist-despite-using-waitfortransform/
        tf_future = tf_buffer.wait_for_transform_async(
                    target_frame = self.target_frame_,
                    source_frame = self.source_frame_,
                    time=rclpy.time.Time()
                )

        rclpy.spin_until_future_complete(self, tf_future)

        try:
            transform = tf_buffer.lookup_transform(self.target_frame_, self.source_frame_, rclpy.time.Time(), timeout = rclpy.duration.Duration(seconds = 5.0))
        except TransformException as ex:
            self.get_logger().warning(f'Could not transform {self.target_frame_} to {self.source_frame_}: {ex}', throttle_duration_sec = 1)
            return False

        # Extract translation and rotation from the transform
        translation = np.array([[transform.transform.translation.x],
                                [transform.transform.translation.y],
                                [transform.transform.translation.z]])

        # quat2mat uses W first!!!
        quat = np.array([transform.transform.rotation.w,
                         transform.transform.rotation.x,
                         transform.transform.rotation.y,
                         transform.transform.rotation.z])

        rotation = quat2mat(quat)
        rotation = np.reshape(rotation, (3,3))

        # Extrinsic matrix from camera reference to base_link reference
        extrinsic_matrix = np.append(rotation, translation, axis=1)
        self.homogeneous_matrix_ = np.r_[extrinsic_matrix, [[0, 0, 0, 1]]]
        self.homogeneous_matrix_ = np.reshape(self.homogeneous_matrix_, (4,4))
        self.get_logger().info("Extrinsic parameters captured")
        return True


    def get_world_coord(self, image_coor):
        """
            Transform the camera coordinates
            into the world coordinates
        """
        # Check array dimensions
        dim = np.shape(self.cv_depth_)
        if image_coor[0] >= dim[0] or image_coor[1] >= dim[1]:
            return []
        # Get camera coordinates
        z = self.cv_depth_[image_coor[1]][image_coor[0]]
        x = z * ((image_coor[0] - self.cx_) / self.fx_)
        y = z * ((image_coor[1] - self.cy_) / self.fy_)
        # Log the values of x, y and z
        #self.get_logger().info(f'Computed x: {x} mm, y: {y} mm, z: {z} mm')
        xyz = np.append(np.array([x, y, z]), 1)
        xyz = np.reshape(xyz, (4,1))
        # Transform the camera coordinates into world coordinates
        world_coordinates = self.homogeneous_matrix_@xyz
        return world_coordinates[:3]
        

def main(args=None):
    rclpy.init(args=args)
    camera_subscriber = HoleDetector()
    rclpy.spin(camera_subscriber)

    rclpy.shutdown()

if __name__ == '__main__':
    main()
