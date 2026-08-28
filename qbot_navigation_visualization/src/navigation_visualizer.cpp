// Copyright 2026 Joseph

#include <pcl/filters/voxel_grid.h>
#include <pcl/point_cloud.h>
#include <pcl/point_types.h>
#include <pcl_conversions/pcl_conversions.h>
#include <tf2/LinearMath/Transform.h>
#include <tf2_ros/buffer.h>
#include <tf2_ros/transform_listener.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include <diagnostic_msgs/msg/diagnostic_array.hpp>
#include <diagnostic_msgs/msg/diagnostic_status.hpp>
#include <diagnostic_msgs/msg/key_value.hpp>
#include <nav_msgs/msg/occupancy_grid.hpp>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>
#include <sensor_msgs/msg/point_cloud2.hpp>
#include <std_msgs/msg/float32.hpp>
#include <tf2_geometry_msgs/tf2_geometry_msgs.hpp>
#include <tf2_sensor_msgs/tf2_sensor_msgs.hpp>

#include "qbot_navigation_visualization/local_grid_builder.hpp"

namespace qbot_navigation_visualization
{

using namespace std::chrono_literals;

class NavigationVisualizer : public rclcpp::Node
{
public:
  NavigationVisualizer()
  : Node("navigation_visualizer"),
    output_frame_(declare_parameter<std::string>("output_frame", "base_link")),
    cloud_topic_(declare_parameter<std::string>("cloud_topic", "/ouster/points_viz")),
    scan_topic_(declare_parameter<std::string>("scan_topic", "/ouster/scan")),
    forward_points_topic_(
      declare_parameter<std::string>("forward_points_topic", "/navigation/forward_points")),
    local_map_topic_(
      declare_parameter<std::string>("local_map_topic", "/navigation/local_map")),
    min_range_(declare_parameter<double>("min_range", 0.3)),
    max_range_(declare_parameter<double>("max_range", 10.0)),
    point_cloud_half_fov_rad_(
      declare_parameter<double>("point_cloud_fov_degrees", 90.0) * M_PI / 360.0),
    voxel_size_(declare_parameter<double>("voxel_size", 0.10)),
    max_cloud_rate_(declare_parameter<double>("max_cloud_rate", 5.0)),
    restamp_output_(declare_parameter<bool>("restamp_output", false)),
    stale_timeout_(declare_parameter<double>("stale_timeout", 1.0)),
    grid_builder_(GridSpec{
      declare_parameter<double>("grid_resolution", 0.10),
      declare_parameter<double>("grid_forward_extent", 10.0),
      declare_parameter<double>("grid_rear_extent", 10.0),
      declare_parameter<double>("grid_lateral_extent", 20.0)})
  {
    if (min_range_ < 0.0 || max_range_ <= min_range_ || voxel_size_ <= 0.0 ||
      max_cloud_rate_ <= 0.0 || point_cloud_half_fov_rad_ <= 0.0 ||
      point_cloud_half_fov_rad_ > M_PI)
    {
      throw std::invalid_argument("Navigation visualization parameters are invalid");
    }

    tf_buffer_ = std::make_unique<tf2_ros::Buffer>(get_clock());
    tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);

    forward_points_pub_ = create_publisher<sensor_msgs::msg::PointCloud2>(
      forward_points_topic_, rclcpp::SensorDataQoS());
    local_map_pub_ = create_publisher<nav_msgs::msg::OccupancyGrid>(local_map_topic_, 2);
    nearest_obstacle_pub_ = create_publisher<std_msgs::msg::Float32>(
      "/navigation/nearest_obstacle", 5);
    diagnostics_pub_ = create_publisher<diagnostic_msgs::msg::DiagnosticArray>(
      "/navigation/diagnostics", 5);

    cloud_sub_ = create_subscription<sensor_msgs::msg::PointCloud2>(
      cloud_topic_, rclcpp::SensorDataQoS(),
      std::bind(&NavigationVisualizer::cloud_callback, this, std::placeholders::_1));
    scan_sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
      scan_topic_, rclcpp::SensorDataQoS(),
      std::bind(&NavigationVisualizer::scan_callback, this, std::placeholders::_1));

    diagnostics_timer_ = create_wall_timer(1s, [this]() {publish_diagnostics();});
    RCLCPP_INFO(
      get_logger(), "Publishing %s and %s in frame %s",
      forward_points_topic_.c_str(), local_map_topic_.c_str(), output_frame_.c_str());
  }

private:
  void cloud_callback(const sensor_msgs::msg::PointCloud2::ConstSharedPtr msg)
  {
    last_cloud_received_ = std::chrono::steady_clock::now();
    ++cloud_messages_;
    const auto minimum_period = std::chrono::duration<double>(1.0 / max_cloud_rate_);
    if (last_cloud_published_.time_since_epoch().count() != 0 &&
      last_cloud_received_ - last_cloud_published_ < minimum_period)
    {
      return;
    }

    sensor_msgs::msg::PointCloud2 cloud_in_base;
    if (msg->header.frame_id == output_frame_) {
      // Bag-only validation can intentionally operate in the sensor frame when
      // no robot-to-sensor mounting transform was recorded.
      cloud_in_base = *msg;
    } else {
      try {
        const auto transform = tf_buffer_->lookupTransform(
          output_frame_, msg->header.frame_id, tf2::TimePointZero, 100ms);
        tf2::doTransform(*msg, cloud_in_base, transform);
      } catch (const tf2::TransformException & error) {
        RCLCPP_WARN_THROTTLE(
          get_logger(), *get_clock(), 5000, "Cannot transform point cloud to %s: %s",
          output_frame_.c_str(), error.what());
        return;
      }
    }

    pcl::PointCloud<pcl::PointXYZI> input_cloud;
    pcl::fromROSMsg(cloud_in_base, input_cloud);
    pcl::PointCloud<pcl::PointXYZI>::Ptr cropped(new pcl::PointCloud<pcl::PointXYZI>());
    cropped->reserve(input_cloud.size());
    for (const auto & point : input_cloud.points) {
      if (!std::isfinite(point.x) || !std::isfinite(point.y) || !std::isfinite(point.z)) {
        continue;
      }
      const double planar_range = std::hypot(point.x, point.y);
      if (point.x <= 0.0 || planar_range < min_range_ || planar_range > max_range_) {
        continue;
      }
      if (std::abs(std::atan2(point.y, point.x)) > point_cloud_half_fov_rad_) {
        continue;
      }
      cropped->push_back(point);
    }
    cropped->width = static_cast<uint32_t>(cropped->size());
    cropped->height = 1;
    cropped->is_dense = false;

    pcl::PointCloud<pcl::PointXYZI> filtered;
    pcl::VoxelGrid<pcl::PointXYZI> voxel_filter;
    voxel_filter.setInputCloud(cropped);
    const float leaf_size = static_cast<float>(voxel_size_);
    voxel_filter.setLeafSize(leaf_size, leaf_size, leaf_size);
    voxel_filter.filter(filtered);

    sensor_msgs::msg::PointCloud2 output;
    pcl::toROSMsg(filtered, output);
    output.header.stamp = msg->header.stamp;
    if (restamp_output_) {
      output.header.stamp = now();
    }
    output.header.frame_id = output_frame_;
    forward_points_pub_->publish(output);
    last_cloud_published_ = last_cloud_received_;
    ++filtered_cloud_messages_;
  }

  void scan_callback(const sensor_msgs::msg::LaserScan::ConstSharedPtr msg)
  {
    last_scan_received_ = std::chrono::steady_clock::now();
    ++scan_messages_;

    tf2::Transform scan_to_base;
    scan_to_base.setIdentity();
    if (msg->header.frame_id != output_frame_) {
      try {
        const auto transform = tf_buffer_->lookupTransform(
          output_frame_, msg->header.frame_id, tf2::TimePointZero, 100ms);
        tf2::fromMsg(transform.transform, scan_to_base);
      } catch (const tf2::TransformException & error) {
        RCLCPP_WARN_THROTTLE(
          get_logger(), *get_clock(), 5000, "Cannot transform scan to %s: %s",
          output_frame_.c_str(), error.what());
        return;
      }
    }

    const tf2::Vector3 sensor_origin_3d = scan_to_base * tf2::Vector3(0.0, 0.0, 0.0);
    const Point2D sensor_origin{sensor_origin_3d.x(), sensor_origin_3d.y()};
    std::vector<Point2D> endpoints;
    endpoints.reserve(msg->ranges.size());
    float nearest = std::numeric_limits<float>::quiet_NaN();

    for (size_t index = 0; index < msg->ranges.size(); ++index) {
      const float range = msg->ranges[index];
      if (!std::isfinite(range) || range < std::max<float>(msg->range_min, min_range_) ||
        range > std::min<float>(msg->range_max, max_range_))
      {
        continue;
      }
      const double angle = msg->angle_min + static_cast<double>(index) * msg->angle_increment;
      const tf2::Vector3 scan_point(range * std::cos(angle), range * std::sin(angle), 0.0);
      const tf2::Vector3 base_point = scan_to_base * scan_point;
      const double bearing = std::atan2(base_point.y(), base_point.x());
      if (base_point.x() <= -grid_builder_.spec().rear_extent ||
        base_point.x() >= grid_builder_.spec().forward_extent ||
        std::abs(base_point.y()) >= grid_builder_.spec().lateral_extent / 2.0)
      {
        continue;
      }
      endpoints.push_back(Point2D{base_point.x(), base_point.y()});
      if (base_point.x() > 0.0 && std::abs(bearing) <= point_cloud_half_fov_rad_) {
        const float base_range = static_cast<float>(std::hypot(base_point.x(), base_point.y()));
        nearest = std::isnan(nearest) ? base_range : std::min(nearest, base_range);
      }
    }

    nav_msgs::msg::OccupancyGrid grid;
    grid.header.stamp = msg->header.stamp;
    if (restamp_output_) {
      grid.header.stamp = now();
    }
    grid.header.frame_id = output_frame_;
    grid.info.map_load_time = grid.header.stamp;
    grid.info.resolution = static_cast<float>(grid_builder_.spec().resolution);
    grid.info.width = grid_builder_.width();
    grid.info.height = grid_builder_.height();
    grid.info.origin.position.x = grid_builder_.origin_x();
    grid.info.origin.position.y = grid_builder_.origin_y();
    grid.info.origin.orientation.w = 1.0;
    grid.data = grid_builder_.build(sensor_origin, endpoints);
    local_map_pub_->publish(grid);

    std_msgs::msg::Float32 nearest_msg;
    nearest_msg.data = nearest;
    nearest_obstacle_pub_->publish(nearest_msg);
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
    const auto now = std::chrono::steady_clock::now();
    const auto age_seconds = [&now](const std::chrono::steady_clock::time_point & stamp) {
        if (stamp.time_since_epoch().count() == 0) {
          return std::numeric_limits<double>::infinity();
        }
        return std::chrono::duration<double>(now - stamp).count();
      };
    const double cloud_age = age_seconds(last_cloud_received_);
    const double scan_age = age_seconds(last_scan_received_);

    diagnostic_msgs::msg::DiagnosticStatus status;
    status.name = "QBot navigation visualization";
    status.hardware_id = "qbot_navigation_visualization";
    status.level = diagnostic_msgs::msg::DiagnosticStatus::OK;
    status.message = "Cloud and scan inputs are current";
    if (cloud_age > stale_timeout_ || scan_age > stale_timeout_) {
      status.level = diagnostic_msgs::msg::DiagnosticStatus::WARN;
      status.message = "Waiting for current Ouster cloud and scan inputs";
    }
    status.values.push_back(key_value("cloud_input_hz", std::to_string(cloud_messages_)));
    status.values.push_back(
      key_value(
        "forward_cloud_hz",
        std::to_string(filtered_cloud_messages_)));
    status.values.push_back(key_value("scan_input_hz", std::to_string(scan_messages_)));
    status.values.push_back(key_value("cloud_age_seconds", std::to_string(cloud_age)));
    status.values.push_back(key_value("scan_age_seconds", std::to_string(scan_age)));

    diagnostic_msgs::msg::DiagnosticArray diagnostics;
    diagnostics.header.stamp = now_ros();
    diagnostics.status.push_back(std::move(status));
    diagnostics_pub_->publish(diagnostics);
    cloud_messages_ = 0;
    filtered_cloud_messages_ = 0;
    scan_messages_ = 0;
  }

  rclcpp::Time now_ros() {return get_clock()->now();}

  std::string output_frame_;
  std::string cloud_topic_;
  std::string scan_topic_;
  std::string forward_points_topic_;
  std::string local_map_topic_;
  double min_range_;
  double max_range_;
  double point_cloud_half_fov_rad_;
  double voxel_size_;
  double max_cloud_rate_;
  bool restamp_output_;
  double stale_timeout_;
  LocalGridBuilder grid_builder_;

  std::unique_ptr<tf2_ros::Buffer> tf_buffer_;
  std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
  rclcpp::Subscription<sensor_msgs::msg::PointCloud2>::SharedPtr cloud_sub_;
  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr scan_sub_;
  rclcpp::Publisher<sensor_msgs::msg::PointCloud2>::SharedPtr forward_points_pub_;
  rclcpp::Publisher<nav_msgs::msg::OccupancyGrid>::SharedPtr local_map_pub_;
  rclcpp::Publisher<std_msgs::msg::Float32>::SharedPtr nearest_obstacle_pub_;
  rclcpp::Publisher<diagnostic_msgs::msg::DiagnosticArray>::SharedPtr diagnostics_pub_;
  rclcpp::TimerBase::SharedPtr diagnostics_timer_;
  std::chrono::steady_clock::time_point last_cloud_received_{};
  std::chrono::steady_clock::time_point last_cloud_published_{};
  std::chrono::steady_clock::time_point last_scan_received_{};
  size_t cloud_messages_{0};
  size_t filtered_cloud_messages_{0};
  size_t scan_messages_{0};
};

}  // namespace qbot_navigation_visualization

int main(int argc, char ** argv)
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<qbot_navigation_visualization::NavigationVisualizer>());
  rclcpp::shutdown();
  return 0;
}
