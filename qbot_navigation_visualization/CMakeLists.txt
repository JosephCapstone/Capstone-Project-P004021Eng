cmake_minimum_required(VERSION 3.8)
project(qbot_navigation_visualization)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(diagnostic_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(PCL REQUIRED COMPONENTS common filters)
find_package(pcl_conversions REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(tf2 REQUIRED)
find_package(tf2_geometry_msgs REQUIRED)
find_package(tf2_ros REQUIRED)
find_package(tf2_sensor_msgs REQUIRED)

add_library(local_grid_builder src/local_grid_builder.cpp)
target_compile_features(local_grid_builder PUBLIC cxx_std_17)
target_include_directories(local_grid_builder PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(local_grid_builder nav_msgs)

add_library(ouster_mapping_adapter_core src/ouster_mapping_adapter_core.cpp)
target_compile_features(ouster_mapping_adapter_core PUBLIC cxx_std_17)
target_include_directories(ouster_mapping_adapter_core PUBLIC
  $<BUILD_INTERFACE:${CMAKE_CURRENT_SOURCE_DIR}/include>
  $<INSTALL_INTERFACE:include>)
ament_target_dependencies(ouster_mapping_adapter_core sensor_msgs)

add_executable(navigation_visualizer src/navigation_visualizer.cpp)
target_compile_features(navigation_visualizer PUBLIC cxx_std_17)
target_include_directories(navigation_visualizer PRIVATE ${PCL_INCLUDE_DIRS})
target_link_libraries(navigation_visualizer local_grid_builder ${PCL_LIBRARIES})
ament_target_dependencies(navigation_visualizer
  diagnostic_msgs
  geometry_msgs
  nav_msgs
  pcl_conversions
  rclcpp
  sensor_msgs
  std_msgs
  tf2
  tf2_geometry_msgs
  tf2_ros
  tf2_sensor_msgs)

add_executable(ouster_mapping_adapter src/ouster_mapping_adapter.cpp)
target_compile_features(ouster_mapping_adapter PUBLIC cxx_std_17)
target_link_libraries(ouster_mapping_adapter ouster_mapping_adapter_core)
ament_target_dependencies(ouster_mapping_adapter
  diagnostic_msgs
  rclcpp
  sensor_msgs)

install(TARGETS
  local_grid_builder
  ouster_mapping_adapter_core
  navigation_visualizer
  ouster_mapping_adapter
  ARCHIVE DESTINATION lib
  LIBRARY DESTINATION lib
  RUNTIME DESTINATION lib/${PROJECT_NAME})
install(DIRECTORY include/ DESTINATION include)
install(DIRECTORY config launch foxglove DESTINATION share/${PROJECT_NAME})
ament_python_install_package(${PROJECT_NAME})
install(PROGRAMS
  scripts/delta_mapping_worker
  DESTINATION lib/${PROJECT_NAME})

if(BUILD_TESTING)
  find_package(ament_cmake_gtest REQUIRED)
  find_package(ament_cmake_pytest REQUIRED)
  ament_add_gtest(test_local_grid_builder test/test_local_grid_builder.cpp)
  target_link_libraries(test_local_grid_builder local_grid_builder)

  ament_add_gtest(test_ouster_mapping_adapter test/test_ouster_mapping_adapter.cpp)
  target_link_libraries(test_ouster_mapping_adapter ouster_mapping_adapter_core)

  ament_add_pytest_test(test_mapping_core test/test_mapping_core.py)

  find_package(ament_lint_auto REQUIRED)
  set(ament_cmake_copyright_FOUND TRUE)
  ament_lint_auto_find_test_dependencies()
endif()

ament_export_include_directories(include)
ament_export_libraries(local_grid_builder ouster_mapping_adapter_core)
ament_package()
