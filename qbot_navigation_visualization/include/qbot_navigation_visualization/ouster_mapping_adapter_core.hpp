// Copyright 2026 Joseph

#pragma once

#include <array>
#include <cstdint>
#include <optional>
#include <string>

#include <sensor_msgs/msg/imu.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>

namespace qbot_navigation_visualization
{

enum class MappingStream
{
  Scan,
  Imu,
};

class SharedTimestampAligner
{
public:
  [[nodiscard]] bool align(
    MappingStream stream, int64_t source_nanoseconds, int64_t current_nanoseconds,
    int64_t & aligned_nanoseconds);

  [[nodiscard]] bool initialized() const;
  [[nodiscard]] int64_t offset_nanoseconds() const;
  [[nodiscard]] uint64_t rejected_messages() const;

private:
  std::optional<int64_t> offset_nanoseconds_;
  std::optional<int64_t> last_scan_nanoseconds_;
  std::optional<int64_t> last_imu_nanoseconds_;
  uint64_t rejected_messages_{0};
};

[[nodiscard]] sensor_msgs::msg::LaserScan normalize_ouster_scan(
  const sensor_msgs::msg::LaserScan & input, const std::string & output_frame,
  double yaw_offset_radians);

[[nodiscard]] sensor_msgs::msg::Imu normalize_ouster_imu(
  const sensor_msgs::msg::Imu & input, const std::string & output_frame,
  double yaw_offset_radians);

}  // namespace qbot_navigation_visualization
