// Copyright 2026 Joseph

#include <chrono>
#include <cmath>
#include <cstdint>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>

#include <diagnostic_msgs/msg/diagnostic_array.hpp>
#include <diagnostic_msgs/msg/diagnostic_status.hpp>
#include <diagnostic_msgs/msg/key_value.hpp>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/imu.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>

#include "qbot_navigation_visualization/ouster_mapping_adapter_core.hpp"

namespace qbot_navigation_visualization
{

using namespace std::chrono_literals;

class OusterMappingAdapter : public rclcpp::Node
{
public:
  OusterMappingAdapter()
  : Node("ouster_mapping_adapter"),
    scan_input_topic_(declare_parameter<std::string>("scan_input_topic", "/ouster/scan")),
    imu_input_topic_(declare_parameter<std::string>("imu_input_topic", "/ouster/imu")),
    scan_output_topic_(
      declare_parameter<std::string>("scan_output_topic", "/navigation/mapping/scan")),
    imu_output_topic_(
      declare_parameter<std::string>("imu_output_topic", "/navigation/mapping/imu")),
    diagnostics_topic_(declare_parameter<std::string>(
        "diagnostics_topic", "/navigation/mapping_diagnostics")),
    mapping_frame_(declare_parameter<std::string>("mapping_frame", "os_lidar")),
    scan_yaw_offset_radians_(
      declare_parameter<double>("scan_yaw_offset_radians", 0.0)),
    imu_yaw_offset_radians_(
      declare_parameter<double>("imu_yaw_offset_radians", M_PI)),
    stale_timeout_seconds_(declare_parameter<double>("stale_timeout", 1.0))
  {
    if (mapping_frame_.empty() || stale_timeout_seconds_ <= 0.0 ||
      !std::isfinite(scan_yaw_offset_radians_) || !std::isfinite(imu_yaw_offset_radians_))
    {
      throw std::invalid_argument("Ouster mapping adapter parameters are invalid");
    }

    scan_pub_ = create_publisher<sensor_msgs::msg::LaserScan>(
      scan_output_topic_, rclcpp::SensorDataQoS());
    imu_pub_ = create_publisher<sensor_msgs::msg::Imu>(
      imu_output_topic_, rclcpp::SensorDataQoS());
    diagnostics_pub_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>(
      diagnostics_topic_, 5);

    scan_sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
      scan_input_topic_, rclcpp::SensorDataQoS(),
      std::bind(&OusterMappingAdapter::scan_callback, this, std::placeholders::_1));
    imu_sub_ = create_subscription<sensor_msgs::msg::Imu>(
      imu_input_topic_, rclcpp::SensorDataQoS(),
      std::bind(&OusterMappingAdapter::imu_callback, this, std::placeholders::_1));

    diagnostics_timer_ = create_wall_timer(1s, [this]() {publish_diagnostics();});
    RCLCPP_INFO(
      get_logger(), "Normalizing %s and %s into mapping frame %s",
      scan_input_topic_.c_str(), imu_input_topic_.c_str(), mapping_frame_.c_str());
  }

private:
  static int64_t stamp_nanoseconds(const builtin_interfaces::msg::Time & stamp)
  {
    return static_cast<int64_t>(stamp.sec) * 1000000000LL + stamp.nanosec;
  }

  bool align_stamp(
    MappingStream stream, const builtin_interfaces::msg::Time & input_stamp,
    builtin_interfaces::msg::Time & output_stamp)
  {
    int64_t aligned_nanoseconds = 0;
    if (!timestamp_aligner_.align(
        stream, stamp_nanoseconds(input_stamp), now().nanoseconds(), aligned_nanoseconds))
    {
      RCLCPP_ERROR_THROTTLE(
        get_logger(), *get_clock(), 5000,
        "Dropping non-monotonic Ouster timestamp; restart mapping after a sensor clock reset");
      return false;
    }
    output_stamp = rclcpp::Time(aligned_nanoseconds);
    return true;
  }

  void scan_callback(const sensor_msgs::msg::LaserScan::ConstSharedPtr input)
  {
    last_scan_received_ = std::chrono::steady_clock::now();
    ++scan_input_messages_;
    try {
      auto output = normalize_ouster_scan(*input, mapping_frame_, scan_yaw_offset_radians_);
      if (!align_stamp(MappingStream::Scan, input->header.stamp, output.header.stamp)) {
        return;
      }
      scan_pub_->publish(output);
      ++scan_output_messages_;
    } catch (const std::exception & error) {
      ++normalization_errors_;
      RCLCPP_ERROR_THROTTLE(
        get_logger(), *get_clock(), 5000, "Cannot normalize Ouster scan: %s", error.what());
    }
  }

  void imu_callback(const sensor_msgs::msg::Imu::ConstSharedPtr input)
  {
    last_imu_received_ = std::chrono::steady_clock::now();
    ++imu_input_messages_;
    auto output = normalize_ouster_imu(*input, mapping_frame_, imu_yaw_offset_radians_);
    if (!align_stamp(MappingStream::Imu, input->header.stamp, output.header.stamp)) {
      return;
    }
    imu_pub_->publish(output);
    ++imu_output_messages_;
  }

  static diagnostic_msgs::msg::KeyValue key_value(
    const std::string & key, const std::string & value)
  {
    diagnostic_msgs::msg::KeyValue item;
    item.key = key;
    item.value = value;
    return item;
  }

  void publish_diagnostics()
  {
    const auto steady_now = std::chrono::steady_clock::now();
    const auto age_seconds = [&steady_now](const std::chrono::steady_clock::time_point & stamp) {
        if (stamp.time_since_epoch().count() == 0) {
          return std::numeric_limits<double>::infinity();
        }
        return std::chrono::duration<double>(steady_now - stamp).count();
      };
    const double scan_age = age_seconds(last_scan_received_);
    const double imu_age = age_seconds(last_imu_received_);

    diagnostic_msgs::msg::DiagnosticStatus status;
    status.name = "Ouster 2D mapping adapter";
    status.hardware_id = "ouster_mapping_adapter";
    status.level = diagnostic_msgs::msg::DiagnosticStatus::OK;
    status.message = "Scan and IMU inputs are current and clock-aligned";
    if (timestamp_aligner_.rejected_messages() > 0 || normalization_errors_ > 0) {
      status.level = diagnostic_msgs::msg::DiagnosticStatus::ERROR;
      status.message = "Mapping input messages were rejected";
    } else {
      if (!timestamp_aligner_.initialized() || scan_age > stale_timeout_seconds_ ||
        imu_age > stale_timeout_seconds_)
      {
        status.level = diagnostic_msgs::msg::DiagnosticStatus::WARN;
        status.message = "Waiting for current Ouster scan and IMU inputs";
      }
    }

    status.values.push_back(key_value("mapping_frame", mapping_frame_));
    status.values.push_back(key_value("scan_input_hz", std::to_string(scan_input_messages_)));
    status.values.push_back(key_value("scan_output_hz", std::to_string(scan_output_messages_)));
    status.values.push_back(key_value("imu_input_hz", std::to_string(imu_input_messages_)));
    status.values.push_back(key_value("imu_output_hz", std::to_string(imu_output_messages_)));
    status.values.push_back(key_value("scan_age_seconds", std::to_string(scan_age)));
    status.values.push_back(key_value("imu_age_seconds", std::to_string(imu_age)));
    status.values.push_back(
      key_value(
        "clock_offset_seconds",
        std::to_string(static_cast<double>(timestamp_aligner_.offset_nanoseconds()) / 1e9)));
    status.values.push_back(
      key_value(
        "rejected_timestamps", std::to_string(timestamp_aligner_.rejected_messages())));
    status.values.push_back(
      key_value(
        "normalization_errors",
        std::to_string(normalization_errors_)));

    diagnostic_msgs::msg::DiagnosticArray diagnostics;
    diagnostics.header.stamp = now();
    diagnostics.status.push_back(std::move(status));
    diagnostics_pub_->publish(diagnostics);

    scan_input_messages_ = 0;
    scan_output_messages_ = 0;
    imu_input_messages_ = 0;
    imu_output_messages_ = 0;
  }

  std::string scan_input_topic_;
  std::string imu_input_topic_;
  std::string scan_output_topic_;
  std::string imu_output_topic_;
  std::string diagnostics_topic_;
  std::string mapping_frame_;
  double scan_yaw_offset_radians_;
  double imu_yaw_offset_radians_;
  double stale_timeout_seconds_;
  SharedTimestampAligner timestamp_aligner_;

  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr scan_sub_;
  rclcpp::Subscription<sensor_msgs::msg::Imu>::SharedPtr imu_sub_;
  rclcpp::Publisher<sensor_msgs::msg::LaserScan>::SharedPtr scan_pub_;
  rclcpp::Publisher<sensor_msgs::msg::Imu>::SharedPtr imu_pub_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_pub_;
  rclcpp::TimerBase::SharedPtr diagnostics_timer_;
  std::chrono::steady_clock::time_point last_scan_received_{};
  std::chrono::steady_clock::time_point last_imu_received_{};
  uint64_t scan_input_messages_{0};
  uint64_t scan_output_messages_{0};
  uint64_t imu_input_messages_{0};
  uint64_t imu_output_messages_{0};
  uint64_t normalization_errors_{0};
};

}  // namespace qbot_navigation_visualization

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<qbot_navigation_visualization::OusterMappingAdapter>());
  rclcpp::shutdown();
  return 0;
}
