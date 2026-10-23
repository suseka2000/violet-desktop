"""Synthetic architecture only: watertight, handed, solid customer geometry."""
import unittest
from copy import deepcopy

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from backend.domain.builders import add_rectangle
from backend.domain.models import Building, Floor, Opening, Point2, Project, ProjectMetadata, Wall
from backend.finish_geometry import outline
from backend.finish_presentation import build_finish_presentation


def fixture():
    floor = Floor(name="Этаж")
    add_rectangle(floor, code="001", name="Синтетическая комната", x=0, y=0,
                  width_mm=4000, depth_mm=2000, height_mm=3000)
    project = Project(metadata=ProjectMetadata(name="Проверка презентации"),
                      buildings=[Building(name="Здание", floors=[floor])])
    project.properties["wall_mounting"] = {"walls": [{"wall_id":w.id, "face_id":f.id,
        "configuration":{"panel_thickness_mm":16, "panel_width_mm":800, "panel_height_mm":3000,
                         "joint_mm":8, "material":"veneer"}}
        for w in floor.walls for f in w.faces if f.space_id]}
    wall = floor.walls[0]
    floor.openings.append(Opening(wall_id=wall.id, kind="door", offset_mm=600, width_mm=900, height_mm=2100))
    return project, floor, wall


def mounted_partition(project, floor, start=(2000, 0), end=(2000, 2000)):
    wall = Wall(start=Point2(x=start[0], y=start[1]), end=Point2(x=end[0], y=end[1]),
                thickness_mm=100, properties={"editor_kind": "partition"})
    floor.walls.append(wall)
    cfg = deepcopy(project.properties["wall_mounting"]["walls"][0]["configuration"])
    for face in wall.faces:
        project.properties["wall_mounting"]["walls"].append({"wall_id": wall.id, "face_id": face.id, "configuration": cfg})
    return wall


def plane(solids, z):
    return unary_union([Polygon(p["footprint_mm"], p.get("holes_mm", [])) for p in solids
                        if p["bottom_mm"] < z < p["bottom_mm"]+p["height_mm"]])


class FinishGeometryTests(unittest.TestCase):
    def test_unmounted_walls_are_solid_and_keep_door_and_window_voids(self):
        project, floor, wall = fixture()
        project.properties.pop("wall_mounting")
        floor.openings.append(Opening(wall_id=wall.id, kind="window", offset_mm=2300,
                                     width_mm=800, sill_mm=900, height_mm=1200))
        scene = build_finish_presentation(project)
        self.assertFalse(scene["panels"])
        self.assertTrue(scene["structures"])
        for z in (100, 1000, 2500):
            section = plane(scene["structures"], z)
            self.assertEqual(section.contains(Point(1000, 0)), z > 2100)
            self.assertEqual(section.contains(Point(2600, 0)), not 900 < z < 2100)
            self.assertTrue(section.contains(Point(100, 0)))
        self.assertEqual(plane(scene["structures"], 2500).area,
                         sum(Polygon(p["footprint_mm"],p["holes_mm"]).area for p in scene["structures"]
                             if p["bottom_mm"] < 2500 < p["bottom_mm"]+p["height_mm"]))

    def test_t_partitions_stop_at_adjacent_finish_not_outside_building(self):
        project, floor, wall = fixture()
        partition = mounted_partition(project, floor)
        before = project.model_dump_json()
        scene = build_finish_presentation(project)
        geometry = [m for p in scene["panels"] if p["wall_id"] == partition.id for m in p["meshes"]]
        self.assertTrue(geometry)
        for piece in geometry:
            minx, miny, maxx, maxy = Polygon(piece["footprint_mm"]).bounds
            self.assertGreaterEqual(miny, wall.thickness_mm/2+80+16-.001)
            self.assertLessEqual(maxy, 2000-wall.thickness_mm/2-80-16+.001)
        for panel in scene["panels"]:
            for m in panel["meshes"]:
                z=m["bottom_mm"]+m["height_mm"]/2
                self.assertLess(Polygon(m["footprint_mm"]).intersection(plane(scene["structures"],z)).area,.001)
        self.assertEqual(before, project.model_dump_json())

    def test_double_sided_partition_keeps_both_assigned_faces_separate(self):
        project, floor, _ = fixture(); partition = mounted_partition(project, floor)
        scene = build_finish_presentation(project)
        for face in partition.faces:
            pieces = [p for p in scene["panels"] if p["face_id"] == face.id]
            self.assertTrue(pieces)
            self.assertTrue(all(p["kind"] == "panel" and p["thickness_mm"] == 16 for p in pieces))
        self.assertTrue(any(p["wall_id"] == partition.id for p in scene["structures"]))

    def test_partition_axis_overshoot_does_not_extrude_a_solid_outside(self):
        project, floor, _=fixture();wall=mounted_partition(project,floor,(2000,-300),(2000,2300))
        scene=build_finish_presentation(project)
        parts=[p for p in scene["structures"] if p["wall_id"]==wall.id]
        self.assertTrue(parts)
        for p in parts:
            ys=[y for x,y in p["footprint_mm"]]
            self.assertGreaterEqual(min(ys),0)
            self.assertLessEqual(max(ys),2000)
        self.assertEqual(wall.start.y,-300)  # Never secretly rewrite the plan.

    def test_different_heights_and_adjacent_openings_do_not_make_holes_in_other_walls(self):
        project, floor, wall = fixture()
        partition = mounted_partition(project, floor, (1000, 0), (1000, 1800))
        partition.height_mm = 1500
        scene = build_finish_presentation(project)
        self.assertTrue(plane(scene["structures"], 1000).contains(Point(1000, 10)))
        self.assertFalse(plane(scene["structures"], 1800).contains(Point(1000, 10)))
        self.assertTrue(plane(scene["structures"], 2500).contains(Point(1000, 0)))

    def test_facade_is_not_clipped_to_interior_envelope(self):
        project, floor, wall = fixture()
        face = next(f for f in wall.faces if not f.space_id)
        project.properties["wall_mounting"]["walls"].append({"wall_id":wall.id,"face_id":face.id,
            "configuration":{"installation_zone":"exterior", "material":"hpl"}})
        scene = build_finish_presentation(project)
        external = [p for p in scene["panels"] if p["face_id"] == face.id]
        self.assertTrue(external)
        self.assertTrue(all(max(y for x,y in m["footprint_mm"]) < -wall.thickness_mm/2
                            for p in external for m in p["meshes"]))

    def test_concave_and_hole_caps_preserve_exact_area(self):
        for polygon in [Polygon([(0,0),(50,0),(50,10),(10,10),(10,50),(0,50)]),
                        Polygon([(0,0),(100,0),(100,100),(0,100)], [[(10,10),(90,10),(90,90),(10,90)]])]:
            result=outline(polygon);triangles=[Polygon(t) for t in result["cap_triangles_mm"]]
            self.assertAlmostEqual(sum(t.area for t in triangles),polygon.area)
            self.assertLess(unary_union(triangles).symmetric_difference(polygon).area,1e-6)

    def test_diagonal_t_partition_clips_actual_geometry(self):
        project, floor, _=fixture();mounted_partition(project,floor,(2000,0),(3000,2000))
        scene=build_finish_presentation(project)
        envelope=Polygon([(0,0),(4000,0),(4000,2000),(0,2000)])
        for p in scene["panels"]:
            for m in p["meshes"]:
                self.assertLess(Polygon(m["footprint_mm"]).difference(envelope).area,1e-6)
                self.assertLess(Polygon(m["footprint_mm"]).intersection(plane(scene["structures"],m["bottom_mm"]+m["height_mm"]/2)).area,1e-6)

    def test_substrate_from_wall_or_mount_settings_not_panel_material(self):
        project, floor, wall = fixture()
        project.properties["whole_wall_settings"]={"walls":[{"wall_id":wall.id,"face_id":wall.faces[0].id,
            "configuration":{"substrate_type":"solid_masonry","cavity_mm":80}}]}
        def material():
            return {p["material"] for p in build_finish_presentation(project)["structures"] if p["wall_id"]==wall.id}
        self.assertEqual(material(),{"solid_masonry"})
        wall.substrate="gypsum";self.assertEqual(material(),{"gypsum"})
        wall.substrate="unspecified"
        project.properties["whole_wall_settings"]["walls"].append({"wall_id":wall.id,"face_id":wall.faces[1].id,
            "configuration":{"substrate_type":"concrete"}})
        self.assertEqual(material(),{"unknown"})
        self.assertTrue(any("разные основания" in w for w in build_finish_presentation(project)["warnings"]))

    def test_panel_colour_override_keeps_other_panels_and_customer_layout_unchanged(self):
        project, floor, _=fixture();scene=build_finish_presentation(project);one=scene["panels"][0]
        project.properties["finish_presentation"]={"panel_colors":{one["id"]:"#123abc"},"panel_surfaces":{one["id"]:"hpl"}}
        scene=build_finish_presentation(project)
        self.assertEqual(sum(p["color"]=="#123abc" for p in scene["panels"]),1)
        view=next(v for v in scene["views"] if v["face_id"]==one["face_id"])
        self.assertEqual(sum(c=="#123abc" for c in view["panel_colors"].values()),1)


if __name__ == "__main__":
    unittest.main()
