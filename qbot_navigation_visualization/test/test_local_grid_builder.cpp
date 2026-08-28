// Copyright 2026 Joseph

#include <gtest/gtest.h>

#include <algorithm>
#include <cstdint>
#include <stdexcept>
#include <vector>

#include "qbot_navigation_visualization/local_grid_builder.hpp"

using qbot_navigation_visualization::GridSpec;
using qbot_navigation_visualization::LocalGridBuilder;
using qbot_navigation_visualization::Point2D;

TEST(LocalGridBuilder, EmptyScanRemainsUnknown)
{
  const LocalGridBuilder builder(GridSpec{0.1, 10.0, 10.0, 20.0});
  const auto cells = builder.build(Point2D{0.0, 0.0}, {});
  EXPECT_EQ(cells.size(), 40000u);
  EXPECT_TRUE(std::all_of(cells.begin(), cells.end(), [](int8_t value) {return value == -1;}));
}

TEST(LocalGridBuilder, RayMarksFreeSpaceAndOccupiedEndpoint)
{
  const LocalGridBuilder builder(GridSpec{0.1, 10.0, 10.0, 20.0});
  const auto cells = builder.build(Point2D{0.0, 0.0}, {Point2D{2.0, 0.0}});
  int start_x = 0;
  int start_y = 0;
  int middle_x = 0;
  int middle_y = 0;
  int end_x = 0;
  int end_y = 0;
  ASSERT_TRUE(builder.world_to_cell(0.0, 0.0, start_x, start_y));
  ASSERT_TRUE(builder.world_to_cell(1.0, 0.0, middle_x, middle_y));
  ASSERT_TRUE(builder.world_to_cell(2.0, 0.0, end_x, end_y));
  EXPECT_EQ(cells[static_cast<size_t>(start_y) * builder.width() + start_x], 0);
  EXPECT_EQ(cells[static_cast<size_t>(middle_y) * builder.width() + middle_x], 0);
  EXPECT_EQ(cells[static_cast<size_t>(end_y) * builder.width() + end_x], 100);
}

TEST(LocalGridBuilder, IgnoresEndpointOutsideGrid)
{
  const LocalGridBuilder builder(GridSpec{0.1, 10.0, 10.0, 20.0});
  const auto cells = builder.build(Point2D{0.0, 0.0}, {Point2D{11.0, 0.0}});
  EXPECT_TRUE(std::all_of(cells.begin(), cells.end(), [](int8_t value) {return value == -1;}));
}

TEST(LocalGridBuilder, RejectsInvalidSpecification)
{
  EXPECT_THROW(LocalGridBuilder(GridSpec{0.0, 10.0, 10.0, 20.0}), std::invalid_argument);
}

TEST(LocalGridBuilder, RobotOriginIsAtGridCentre)
{
  const LocalGridBuilder builder(GridSpec{0.1, 10.0, 10.0, 20.0});
  int cell_x = 0;
  int cell_y = 0;
  ASSERT_TRUE(builder.world_to_cell(0.0, 0.0, cell_x, cell_y));
  EXPECT_EQ(cell_x, 100);
  EXPECT_EQ(cell_y, 100);
  EXPECT_DOUBLE_EQ(builder.origin_x(), -10.0);
  EXPECT_DOUBLE_EQ(builder.origin_y(), -10.0);
}

TEST(LocalGridBuilder, MarksOccupiedEndpointBehindRobot)
{
  const LocalGridBuilder builder(GridSpec{0.1, 10.0, 10.0, 20.0});
  const auto cells = builder.build(Point2D{0.0, 0.0}, {Point2D{-2.0, 0.0}});
  int endpoint_x = 0;
  int endpoint_y = 0;
  ASSERT_TRUE(builder.world_to_cell(-2.0, 0.0, endpoint_x, endpoint_y));
  EXPECT_EQ(cells[static_cast<size_t>(endpoint_y) * builder.width() + endpoint_x], 100);
}
