from typing import List
import numpy as np
import math
from l5kit.configs.config import load_metadata
from l5kit.data import MapAPI, DataManager
from l5kit.data.map_api import InterpolationMethod
from l5kit.data.proto.road_network_pb2 import Lane, MapElement
from l5kit.rasterization.semantic_rasterizer import indices_in_bounds
from queue import PriorityQueue
from collections import deque


class CustomMapAPI(MapAPI):
    def __init__(self, cfg: dict, dm: DataManager):
        dataset_meta_key = cfg["raster_params"]["dataset_meta_key"]
        dataset_meta = load_metadata(dm.require(dataset_meta_key))
        world_to_ecef = np.array(dataset_meta["world_to_ecef"], dtype=np.float64)
        protobuf_map_path = dm.require(cfg["raster_params"]["semantic_map_key"])
        super().__init__(protobuf_map_path, world_to_ecef)

        self.lane_cfg_params = cfg["data_generation_params"]["lane_params"]

    def get_closest_lanes_ids(self, position: np.ndarray) -> List[str]:
        """Gets the closest lane of the given position.

        Args:
            position (np.ndarray): The position to get the closest lane of.

        Returns:
            str: The lane id of the closest lane to the given position.
        """
        max_lane_distance = self.lane_cfg_params["max_retrieval_distance_m"]  # Only consider lanes within this distance to the given position.

        # filter first by bounds and then by distance, so that we always take the closest lanes
        lanes_ids = self.bounds_info["lanes"]["ids"]
        lanes_bounds = self.bounds_info["lanes"]["bounds"]
        lanes_indices = indices_in_bounds(position, lanes_bounds, max_lane_distance)
        if len(lanes_indices) == 0:
            return None  # There are no lanes close to the given position, return None.
        
        lanes_distances = []
        for lane_idx in lanes_indices:
            lane_id = lanes_ids[lane_idx]  # Get the id of the line index (id != index, id is a string, index and integer index of a list).
            closest_midpoints = self.get_closest_lane_midpoints(position, lane_id)  # Determine closest lane midpoints to the given position.
            closest_midpoint = closest_midpoints[0]  # The closest midpoint is the first element of the sorted list of lane midpoints.
            closest_midpoint_distance = np.linalg.norm(closest_midpoint - position)  # Determine distance from the closest midpoint to the given position.
            lanes_distances.append(closest_midpoint_distance)  # Assign the lane distance to be the closest midpoint to the given position.
        lanes_indices = lanes_indices[np.argsort(lanes_distances)]  # Sort the lane indices by lane distance, ascending.
        
        closest_lanes_ids = np.take(lanes_ids, lanes_indices)  # Get the ids of the sorted list of lane indices.
        return closest_lanes_ids

    def get_route(self, trajectory: np.ndarray) -> List[str]:
        visited_lanes_ids = {}
        unvisited_lanes_ids = {}
        
        initial_position = trajectory[0]
        final_position = trajectory[-2]
        
        initial_lanes_ids = self.get_lanes_ids_at(initial_position)
        final_lanes_ids = self.get_lanes_ids_at(final_position)
        
        for initial_lane_id in initial_lanes_ids:
            unvisited_lanes_ids[initial_lane_id] = None
            
        for frame_idx, position in enumerate(trajectory):
            # Skip the first frame.
            if frame_idx == 0: continue
                
            # Get the set of unvisited lanes at this position.
            current_lanes_ids = []
            for lane_id in unvisited_lanes_ids:
                lane_bounds = self.get_lane_bounds(lane_id)
                if not self.in_bounds(position, lane_bounds): continue
                current_lanes_ids.append(lane_id)
            

            for lane_id in current_lanes_ids:
                # Visit this lane (add to set of visited lanes and remove from set of unvisited lanes).
                visited_lanes_ids[lane_id] = unvisited_lanes_ids[lane_id]
                del unvisited_lanes_ids[lane_id]
                
                # Add all connected lanes of the lane at this position to the set of unvisited lanes, setting
                # this lane as their parent.
                connected_lanes_ids = self.get_connected_lanes_ids(lane_id)
                for connected_lane_id in connected_lanes_ids:
                    if connected_lane_id not in visited_lanes_ids:
                        unvisited_lanes_ids[connected_lane_id] = lane_id

        route = None
        for final_lane_id in final_lanes_ids:
            if final_lane_id not in visited_lanes_ids: continue
            route = deque()
            lane_id = final_lane_id
            while not any(x in initial_lanes_ids for x in route):
                route.appendleft(lane_id)  # Add the lane id to the route.
                lane_id = visited_lanes_ids[lane_id]  # Set the lane id as the parent of the current lane.
            break
        return route

    def get_lanes_ids_at(self, position: np.ndarray) -> List[str]:
        """Gets the lanes at the given position.

        Args:
            position (np.ndarray): The position to get the lanes at.

        Returns:
            str: The lane ids of the lanes at the given position.
        """
        lanes_ids = []
        for element in self.elements:
            if not self.is_lane(element): continue
            lane_id = self.id_as_str(element.id)
            lane_bounds = self.get_lane_bounds(lane_id)
            if not self.in_bounds(position, lane_bounds): continue
            lanes_ids.append(lane_id)
        return lanes_ids
            

    def get_closest_lane_midpoints(self, position: np.ndarray, lane_id: str) -> np.ndarray:
        """Gets a sorted list (ascending) of midpoints of the lane, defined by the lane id, that are closest to the given position.

        Args:
            position (np.ndarray): The position to get the closest midpoints to.
            lane_id (str): The id of the lane to get the closest midpoints of.

        Returns:
            np.ndarray: A sorted list (ascending) of midpoints, that are closest to the given position.
        """
        #max_lane_points = self.lane_cfg_params["max_points_per_lane"]  # Maximum amounts of points to represent lane.
        interpolation_method = InterpolationMethod.INTER_METER  # Split lane in a fixed number of points.
        lane = self.get_lane_as_interpolation(lane_id, 1.0, interpolation_method)
        midpoints = lane["xyz_midlane"][:, :2]  # Retrieve the lane's midpoints.
        midpoints_distance = np.linalg.norm(midpoints - position, axis=-1)  # Determine the distance between each midpoint and the given position.
        closest_midpoints = midpoints[np.argsort(midpoints_distance)]  # Sort the midpoints by midpoint distance to the given position, ascending.
        return closest_midpoints
    
    def get_element(self, element_id: str) -> MapElement:
        element_idx = self.ids_to_el[element_id]
        element = self.elements[element_idx]
        return element
    
    def get_lane(self, lane_id: str) -> Lane:
        element = self.get_element(lane_id)
        return element.element.lane

    def get_connected_lanes_ids(self, lane_id: str) -> List[str]:
        ahead_lanes_ids = self.get_ahead_lanes_ids(lane_id)
        change_lanes_ids = self.get_change_lanes_ids(lane_id)
        return ahead_lanes_ids + change_lanes_ids

    def get_ahead_lanes_ids(self, lane_id: str) -> List[str]:
        lane = self.get_lane(lane_id)
        ahead_lanes_ids = []
        for ahead_lane in lane.lanes_ahead:
            ahead_lane_id = self.id_as_str(ahead_lane)
            if bool(ahead_lane_id):
                ahead_lanes_ids.append(ahead_lane_id)
        return ahead_lanes_ids

    def get_change_lanes_ids(self, lane_id: str) -> List[str]:
        lane = self.get_lane(lane_id)
        change_lanes_ids = []
        change_left_lane_id = self.id_as_str(lane.adjacent_lane_change_left)
        change_right_lane_id = self.id_as_str(lane.adjacent_lane_change_right)
        if bool(change_left_lane_id):
            change_lanes_ids.append(change_left_lane_id)
        if bool(change_right_lane_id):
            change_lanes_ids.append(change_right_lane_id)
        return change_lanes_ids

    def get_lane_bounds(self, lane_id: str) -> np.ndarray:
        lane_coords = self.get_lane_coords(lane_id)
        
        # Get the lane bounds.
        x_min = min(np.min(lane_coords["xyz_left"][:, 0]), np.min(lane_coords["xyz_right"][:, 0]))
        y_min = min(np.min(lane_coords["xyz_left"][:, 1]), np.min(lane_coords["xyz_right"][:, 1]))
        x_max = max(np.max(lane_coords["xyz_left"][:, 0]), np.max(lane_coords["xyz_right"][:, 0]))
        y_max = max(np.max(lane_coords["xyz_left"][:, 1]), np.max(lane_coords["xyz_right"][:, 1]))

        bounds = np.asarray([[x_min, y_min], [x_max, y_max]])
        return bounds

    def in_bounds(self, position: np.ndarray, bounds: np.ndarray) -> bool:
        x = position[0]
        y = position[1]
        
        # Get the lane bounds.
        x_min = bounds[0][0]
        y_min = bounds[0][1]
        x_max = bounds[1][0]
        y_max = bounds[1][1]
        
        x_in = x > x_min and x < x_max
        y_in = y > y_min and y < y_max
        
        in_bounds = x_in and y_in
        return in_bounds

    def get_approx_lane_length(self, lane_id: str) -> float:
        bounds = self.get_lane_bounds(lane_id)
        
        # Get the lane bounds.
        x_min = bounds[0][0]
        y_min = bounds[0][1]
        x_max = bounds[1][0]
        y_max = bounds[1][1]
        
        # Approximate the lane length as the lane bounds diagional length.
        x_length = abs(x_max - x_min)
        y_length = abs(y_max - y_min)
        
        lane_length = math.hypot(x_length, y_length)
        return lane_length
    
    def get_lane_progress(self, position: np.ndarray, lane_id: str) -> float:
        """Gets a sorted list (ascending) of midpoints of the lane, defined by the lane id, that are closest to the given position.

        Args:
            position (np.ndarray): The position to get the closest midpoints to.
            lane_id (str): The id of the lane to get the closest midpoints of.

        Returns:
            np.ndarray: A sorted list (ascending) of midpoints, that are closest to the given position.
        """
        max_lane_points = self.lane_cfg_params["max_points_per_lane"]  # Maximum amounts of points to represent lane.
        interpolation_method = InterpolationMethod.INTER_ENSURE_LEN  # Split lane in a fixed number of points.
        lane = self.get_lane_as_interpolation(lane_id, max_lane_points, interpolation_method)
        midpoints = lane["xyz_midlane"][:, :2]  # Retrieve the lane's midpoints.
        progress = np.linspace(0, 1, len(midpoints))
        midpoints_distance = np.linalg.norm(midpoints - position, axis=-1)  # Determine the distance between each midpoint and the given position.
        progress = progress[np.argsort(midpoints_distance)]  # Sort the midpoints by midpoint distance to the given position, ascending.
        return progress[0]
