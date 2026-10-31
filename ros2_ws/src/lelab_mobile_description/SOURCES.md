# Model source and adaptation record

Geometry, inertials and joint layout are adapted from the local reference
`/home/liaozhengyan/Desktop/resource/uavociety/landerpi_description`
(no Git metadata and no license file were present in that directory; this is
an internal research adaptation, not a redistribution claim).

Changes from the reference:
- package/mesh namespace renamed from `landerpi_description` to `lelab_mobile_description`;
- wheel joint limits changed from zero effort/velocity to driveable values;
- base link mass/inertia increased so the light reference model remains stable
  while a small object is grasped;
- the old auxiliary centre-drive mechanism (`wheel_base.urdf.xacro`) and its
  missing `HolonomicPlugin` are not carried over; the four wheel joints are
  driven by Gazebo Harmonic `MecanumDrive`;
- Gazebo Classic/Ignition plugins are replaced by Harmonic `gz_ros2_control`
  and `ros_gz` bridge configuration in `lelab_mobile_sim`;
- wrist RGB-D, lidar and IMU sensor definitions are added in this package and
  the `/clock` bridge lives in the launch package.

The original mesh files are byte-identical copies.
