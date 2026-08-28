// Copyright 2026 Joseph

#include "qbot_navigation_visualization/local_grid_builder.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

namespace qbot_navigation_visualization
{

LocalGridBuilder::LocalGridBuilder(GridSpec spec)
: spec_(spec),
  width_(static_cast<uint32_t>(
      std::lround((spec.forward_extent + spec.rear_extent) / spec.resolution))),
  height_(static_cast<uint32_t>(std::lround(spec.lateral_extent / spec.resolution)))
{
  if (spec_.resolution <= 0.0 || spec_.forward_extent <= 0.0 || spec_.rear_extent < 0.0 ||
    spec_.lateral_extent <= 0.0)
  {
    throw std::invalid_argument("Grid dimensions and resolution must be positive");
  }
}

uint32_t LocalGridBuilder::width() const {return width_;}
uint32_t LocalGridBuilder::height() const {return height_;}
double LocalGridBuilder::origin_x() const {return -spec_.rear_extent;}
double LocalGridBuilder::origin_y() const {return -spec_.lateral_extent / 2.0;}
const GridSpec & LocalGridBuilder::spec() const {return spec_;}

bool LocalGridBuilder::world_to_cell(double x, double y, int & cell_x, int & cell_y) const
{
  cell_x = static_cast<int>(std::floor((x - origin_x()) / spec_.resolution));
  cell_y = static_cast<int>(std::floor((y - origin_y()) / spec_.resolution));
  return cell_x >= 0 && cell_y >= 0 &&
         cell_x < static_cast<int>(width_) && cell_y < static_cast<int>(height_);
}

std::vector<int8_t> LocalGridBuilder::build(
  const Point2D & sensor_origin,
  const std::vector<Point2D> & occupied_endpoints) const
{
  std::vector<int8_t> cells(static_cast<size_t>(width_) * height_, -1);
  int origin_cell_x = 0;
  int origin_cell_y = 0;
  if (!world_to_cell(sensor_origin.x, sensor_origin.y, origin_cell_x, origin_cell_y)) {
    return cells;
  }

  for (const auto & endpoint : occupied_endpoints) {
    int endpoint_cell_x = 0;
    int endpoint_cell_y = 0;
    if (!world_to_cell(endpoint.x, endpoint.y, endpoint_cell_x, endpoint_cell_y)) {
      continue;
    }
    trace_ray(origin_cell_x, origin_cell_y, endpoint_cell_x, endpoint_cell_y, cells);
    cells[static_cast<size_t>(endpoint_cell_y) * width_ + endpoint_cell_x] = 100;
  }
  return cells;
}

void LocalGridBuilder::trace_ray(
  int start_x, int start_y, int end_x, int end_y,
  std::vector<int8_t> & cells) const
{
  int x = start_x;
  int y = start_y;
  const int dx = std::abs(end_x - start_x);
  const int sx = start_x < end_x ? 1 : -1;
  const int dy = -std::abs(end_y - start_y);
  const int sy = start_y < end_y ? 1 : -1;
  int error = dx + dy;

  while (true) {
    const size_t index = static_cast<size_t>(y) * width_ + x;
    if (cells[index] != 100) {
      cells[index] = 0;
    }
    if (x == end_x && y == end_y) {
      break;
    }
    const int doubled_error = 2 * error;
    if (doubled_error >= dy) {
      error += dy;
      x += sx;
    }
    if (doubled_error <= dx) {
      error += dx;
      y += sy;
    }
  }
}

}  // namespace qbot_navigation_visualization
