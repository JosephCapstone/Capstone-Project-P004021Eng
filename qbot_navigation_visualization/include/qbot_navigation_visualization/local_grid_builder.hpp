// Copyright 2026 Joseph

#pragma once

#include <cstdint>
#include <vector>

namespace qbot_navigation_visualization
{

struct Point2D
{
  double x;
  double y;
};

struct GridSpec
{
  double resolution{0.1};
  double forward_extent{10.0};
  double rear_extent{10.0};
  double lateral_extent{20.0};
};

class LocalGridBuilder
{
public:
  explicit LocalGridBuilder(GridSpec spec);

  [[nodiscard]] uint32_t width() const;
  [[nodiscard]] uint32_t height() const;
  [[nodiscard]] double origin_x() const;
  [[nodiscard]] double origin_y() const;
  [[nodiscard]] const GridSpec & spec() const;

  [[nodiscard]] std::vector<int8_t> build(
    const Point2D & sensor_origin,
    const std::vector<Point2D> & occupied_endpoints) const;

  [[nodiscard]] bool world_to_cell(double x, double y, int & cell_x, int & cell_y) const;

private:
  void trace_ray(
    int start_x, int start_y, int end_x, int end_y,
    std::vector<int8_t> & cells) const;

  GridSpec spec_;
  uint32_t width_;
  uint32_t height_;
};

}  // namespace qbot_navigation_visualization
