// Copyright 2026 Joseph

#include "qbot_navigation_visualization/ouster_mapping_adapter_core.hpp"

#include <cmath>
#include <limits>
#include <stdexcept>

namespace qbot_navigation_visualization
{

bool SharedTimestampAligner::align(
  MappingStream stream, int64_t source_nanoseconds, int64_t current_nanoseconds,
  int64_t & aligned_nanoseconds)
{
  auto & last_timestamp =
    stream == MappingStream::Scan ? last_scan_nanoseconds_ : last_imu_nanoseconds_;
  if (source_nanoseconds < 0 ||
    (last_timestamp.has_value() && source_nanoseconds <= *last_timestamp))
  {
    ++rejected_messages_;
    return false;
  }

  if (!offset_nanoseconds_.has_value()) {
    offset_nanoseconds_ = current_nanoseconds - source_nanoseconds;
  }

  if ((*offset_nanoseconds_ > 0 &&
    source_nanoseconds > std::numeric_limits<int64_t>::max() - *offset_nanoseconds_) ||
    (*offset_nanoseconds_ < 0 &&
    source_nanoseconds < std::numeric_limits<int64_t>::min() - *offset_nanoseconds_))
  {
    ++rejected_messages_;
    return false;
  }

  aligned_nanoseconds = source_nanoseconds + *offset_nanoseconds_;
  last_timestamp = source_nanoseconds;
  return true;
}

bool SharedTimestampAligner::initialized() const {return offset_nanoseconds_.has_value();}

int64_t SharedTimestampAligner::offset_nanoseconds() const
{
  return offset_nanoseconds_.value_or(0);
}

uint64_t SharedTimestampAligner::rejected_messages() const {return rejected_messages_;}

sensor_msgs::msg::LaserScan normalize_ouster_scan(
  const sensor_msgs::msg::LaserScan & input, const std::string & output_frame,
  double yaw_offset_radians)
{
  if (!std::isfinite(input.angle_min) || !std::isfinite(input.angle_max) ||
    !std::isfinite(input.angle_increment) || input.angle_increment <= 0.0F ||
    !std::isfinite(yaw_offset_radians))
  {
    throw std::invalid_argument("Laser scan angles and yaw offset must be finite");
  }
  if (!input.intensities.empty() && input.intensities.size() != input.ranges.size()) {
    throw std::invalid_argument("Laser scan ranges and intensities must have matching sizes");
  }

  auto output = input;
  output.header.frame_id = output_frame;
  // Preserve acquisition order so time_increment remains valid. Circularly
  // shifting a full revolution would put a 50 ms discontinuity in this 10 Hz
  // scan and break Cartographer's per-ray motion compensation.
  output.angle_min = static_cast<float>(input.angle_min + yaw_offset_radians);
  output.angle_max = static_cast<float>(input.angle_max + yaw_offset_radians);
  return output;
}

sensor_msgs::msg::Imu normalize_ouster_imu(
  const sensor_msgs::msg::Imu & input, const std::string & output_frame,
  double yaw_offset_radians)
{
  if (!std::isfinite(yaw_offset_radians)) {
    throw std::invalid_argument("IMU yaw offset must be finite");
  }

  auto output = input;
  output.header.frame_id = output_frame;
  const double cosine = std::cos(yaw_offset_radians);
  const double sine = std::sin(yaw_offset_radians);
  const auto rotate_vector = [cosine, sine](const geometry_msgs::msg::Vector3 & vector) {
      geometry_msgs::msg::Vector3 rotated;
      rotated.x = cosine * vector.x - sine * vector.y;
      rotated.y = sine * vector.x + cosine * vector.y;
      rotated.z = vector.z;
      return rotated;
    };
  const auto rotate_covariance = [cosine, sine](const std::array<double, 9> & covariance) {
      if (covariance[0] < 0.0) {
        return covariance;
      }
      const double rotation[3][3] = {
        {cosine, -sine, 0.0},
        {sine, cosine, 0.0},
        {0.0, 0.0, 1.0},
      };
      std::array<double, 9> rotated{};
      for (size_t row = 0; row < 3; ++row) {
        for (size_t column = 0; column < 3; ++column) {
          for (size_t left = 0; left < 3; ++left) {
            for (size_t right = 0; right < 3; ++right) {
              rotated[row * 3 + column] +=
                rotation[row][left] * covariance[left * 3 + right] *
                rotation[column][right];
            }
          }
        }
      }
      return rotated;
    };

  output.angular_velocity = rotate_vector(input.angular_velocity);
  output.linear_acceleration = rotate_vector(input.linear_acceleration);
  output.angular_velocity_covariance =
    rotate_covariance(input.angular_velocity_covariance);
  output.linear_acceleration_covariance =
    rotate_covariance(input.linear_acceleration_covariance);
  return output;
}

}  // namespace qbot_navigation_visualization
