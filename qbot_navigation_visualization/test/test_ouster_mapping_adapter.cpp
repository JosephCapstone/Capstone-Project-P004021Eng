// Copyright 2026 Joseph

#include <gtest/gtest.h>

#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include <sensor_msgs/msg/imu.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>

#include "qbot_navigation_visualization/ouster_mapping_adapter_core.hpp"

using qbot_navigation_visualization::MappingStream;
using qbot_navigation_visualization::SharedTimestampAligner;
using qbot_navigation_visualization::normalize_ouster_imu;
using qbot_navigation_visualization::normalize_ouster_scan;

TEST(SharedTimestampAligner, PreservesRelativeTimeAcrossStreams)
{
  SharedTimestampAligner aligner;
  int64_t output = 0;
  ASSERT_TRUE(aligner.align(MappingStream::Scan, 1000000000LL, 10000000000LL, output));
  EXPECT_EQ(output, 10000000000LL);
  ASSERT_TRUE(aligner.align(MappingStream::Imu, 1100000000LL, 10150000000LL, output));
  EXPECT_EQ(output, 10100000000LL);
  EXPECT_EQ(aligner.offset_nanoseconds(), 9000000000LL);
  EXPECT_EQ(aligner.rejected_messages(), 0u);
}

TEST(SharedTimestampAligner, RejectsPerStreamTimestampRegression)
{
  SharedTimestampAligner aligner;
  int64_t output = 0;
  ASSERT_TRUE(aligner.align(MappingStream::Scan, 200, 1000, output));
  EXPECT_FALSE(aligner.align(MappingStream::Scan, 199, 1100, output));
  EXPECT_FALSE(aligner.align(MappingStream::Scan, 200, 1200, output));
  EXPECT_TRUE(aligner.align(MappingStream::Imu, 150, 1300, output));
  EXPECT_EQ(aligner.rejected_messages(), 2u);
}

TEST(OusterMappingNormalization, RotatesAnglesWhilePreservingAcquisitionOrder)
{
  sensor_msgs::msg::LaserScan input;
  input.header.frame_id = "os_lidar";
  input.header.stamp.sec = 12;
  input.header.stamp.nanosec = 34;
  input.angle_min = -static_cast<float>(M_PI);
  input.angle_max = static_cast<float>(M_PI);
  input.angle_increment = static_cast<float>(M_PI / 2.0);
  input.time_increment = 0.025F;
  input.ranges = {0.0F, 1.0F, 2.0F, 3.0F};
  input.intensities = {10.0F, 11.0F, 12.0F, 13.0F};

  const auto output = normalize_ouster_scan(input, "os_sensor", M_PI);
  EXPECT_EQ(output.header.frame_id, "os_sensor");
  EXPECT_EQ(output.header.stamp, input.header.stamp);
  EXPECT_EQ(output.ranges, input.ranges);
  EXPECT_EQ(output.intensities, input.intensities);
  EXPECT_NEAR(output.angle_min, 0.0F, 1e-6F);
  EXPECT_NEAR(output.angle_max, 2.0F * static_cast<float>(M_PI), 1e-6F);
  EXPECT_FLOAT_EQ(output.angle_increment, input.angle_increment);
  EXPECT_FLOAT_EQ(output.time_increment, input.time_increment);
}

TEST(OusterMappingNormalization, SupportsYawThatIsNotAnIntegerSampleShift)
{
  sensor_msgs::msg::LaserScan input;
  input.angle_min = -1.0F;
  input.angle_max = 1.0F;
  input.angle_increment = 1.0F;
  input.ranges = {1.0F, 2.0F};
  const auto output = normalize_ouster_scan(input, "os_sensor", 0.5);
  EXPECT_FLOAT_EQ(output.angle_min, -0.5F);
  EXPECT_FLOAT_EQ(output.angle_max, 1.5F);
  EXPECT_EQ(output.ranges, input.ranges);
}

TEST(OusterMappingNormalization, PlacesImuInCommonMappingFrame)
{
  sensor_msgs::msg::Imu input;
  input.header.frame_id = "os_imu";
  input.angular_velocity.x = 0.25;
  input.angular_velocity.y = -0.5;
  input.angular_velocity.z = 0.42;
  input.linear_acceleration.x = 0.1;
  input.linear_acceleration.y = 0.2;
  input.linear_acceleration.z = 9.81;
  input.angular_velocity_covariance[0] = 0.1;
  input.angular_velocity_covariance[4] = 0.2;
  input.angular_velocity_covariance[8] = 0.3;

  const auto output = normalize_ouster_imu(input, "os_lidar", M_PI);
  EXPECT_EQ(output.header.frame_id, "os_lidar");
  EXPECT_NEAR(output.angular_velocity.x, -0.25, 1e-12);
  EXPECT_NEAR(output.angular_velocity.y, 0.5, 1e-12);
  EXPECT_DOUBLE_EQ(output.angular_velocity.z, 0.42);
  EXPECT_NEAR(output.linear_acceleration.x, -0.1, 1e-12);
  EXPECT_NEAR(output.linear_acceleration.y, -0.2, 1e-12);
  EXPECT_DOUBLE_EQ(output.linear_acceleration.z, 9.81);
  EXPECT_NEAR(output.angular_velocity_covariance[0], 0.1, 1e-12);
  EXPECT_NEAR(output.angular_velocity_covariance[4], 0.2, 1e-12);
  EXPECT_NEAR(output.angular_velocity_covariance[8], 0.3, 1e-12);
}
