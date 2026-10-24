"""Synthetic checks of visible joints and floor occlusion, without CAD jobs."""
import unittest

from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from backend.assembly_jobs import input_signature
from backend.domain.builders import add_polygon_space, add_rectangle
from backend.domain.models import Floor, LevelDatum, Obstacle, Point2
from backend.finish_presentation import Appearance, build_finish_presentation
from tests.test_finish_geometry import fixture, mounted_partition


def meshes(parts):
    return [m for part in parts for m in part.get("meshes", [part])]


def overlap(a, b):
    height = min(a["bottom_mm"]+a["height_mm"], b["bottom_mm"]+b["height_mm"])-max(a["bottom_mm"], b["bottom_mm"])
    return 0 if height <= 1e-6 else height*Polygon(a["footprint_mm"], a.get("holes_mm", [])).intersection(
        Polygon(b["footprint_mm"], b.get("holes_mm", []))).area


class FinishSurfacesTests(unittest.TestCase):
    def project(self, *, gap=2, rule=None, system="open_mount"):
        project, floor, wall = fixture()
        for binding in project.properties["wall_mounting"]["walls"]:
            binding["configuration"].update(joint_mm=gap, panel_height_mm=1000,
                mounting_system=system, assembly_rule_id=rule)
        return project, floor, wall

    def test_cap_width_does_not_change_actual_panel_gap_or_source(self):
        project, _, wall = self.project()
        before = project.model_dump_json()
        scene = build_finish_presentation(project)
        view = next(v for v in scene["views"] if v["wall_id"] != wall.id)
        self.assertEqual(view["joint_profile"]["width_mm"], 6)
        self.assertEqual(view["joint_profile"]["family"], "V-T1")
        row = sorted((p for p in view["panels"] if p["y_mm"] == 2004), key=lambda p: p["x_mm"])
        self.assertTrue(row)
        for a, b in zip(row, row[1:]):
            self.assertAlmostEqual(b["x_mm"]-a["x_mm"]-a["width_mm"], 2)
        for p in meshes(scene["profiles"]):
            for panel in meshes(scene["panels"]):
                self.assertLess(overlap(p, panel), .001)
        self.assertEqual(project.model_dump_json(), before)

    def test_profiles_are_cut_at_openings_and_crossings_not_fragment_edges(self):
        project, _, _ = self.project()
        scene = build_finish_presentation(project)
        for v in scene["views"]:
            strips = [box(r["x_mm"], r["y_mm"], r["x_mm"]+r["width_mm"], r["y_mm"]+r["height_mm"]) for r in v["profile_strips"]]
            self.assertAlmostEqual(sum(p.area for p in strips), unary_union(strips).area, places=4)
            for o in v["openings"]:
                opening = box(o["x_mm"], o["y_mm"], o["x_mm"]+o["width_mm"], o["y_mm"]+o["height_mm"])
                self.assertLess(unary_union(strips).intersection(opening).area, .001)
        profiles = meshes(scene["profiles"])
        for i, a in enumerate(profiles):
            for b in profiles[i+1:]:
                self.assertLess(overlap(a, b), .001)
        self.assertEqual({p["orientation"] for p in scene["profiles"]}, {"vertical", "horizontal"})

    def test_hidden_profile_recess_and_zero_joint(self):
        for rule, gap in (("concealed-2-1", 17), ("concealed-2-4", 4)):
            project, _, _ = self.project(gap=gap, rule=rule, system="concealed_mount")
            scene = build_finish_presentation(project)
            self.assertTrue(scene["profiles"])
            for view in scene["views"]:
                self.assertEqual(view["joint_profile"]["front_mm"], -5.5)
                self.assertEqual(view["joint_profile"]["width_mm"], gap)
            for profile in meshes(scene["profiles"]):
                for panel in meshes(scene["panels"]):
                    self.assertLess(overlap(profile, panel), .001)
        project, _, _ = self.project(gap=0)
        self.assertEqual(build_finish_presentation(project)["profiles"], [])

    def test_joint_group_override_not_floor_or_ceiling_rule(self):
        project, _, _ = self.project(rule="concealed-2-1")
        for b in project.properties["wall_mounting"]["walls"]:
            b["configuration"]["node_rule_ids"] = {"joints": "open-1-1", "floor": "concealed-2-4"}
        self.assertEqual({v["joint_profile"]["family"] for v in build_finish_presentation(project)["views"]}, {"V-T1"})

    def test_t_joints_and_partition_overshoot_clip_profiles_too(self):
        project, floor, _ = self.project()
        partition = mounted_partition(project, floor, (2000, -300), (2000, 2300))
        scene = build_finish_presentation(project)
        parts = [p for p in scene["profiles"] if p["wall_id"] == partition.id]
        self.assertTrue(parts)
        all_profiles = meshes(scene["profiles"])
        for i, a in enumerate(all_profiles):
            for b in all_profiles[i+1:]:
                self.assertLess(overlap(a, b), .001)
        for part in meshes(parts):
            self.assertGreaterEqual(Polygon(part["footprint_mm"]).bounds[1], 0)
            self.assertLessEqual(Polygon(part["footprint_mm"]).bounds[3], 2000)
            for base in scene["structures"]:
                self.assertLess(overlap(part, base), .001)

    def test_opaque_floor_follows_room_and_is_below_its_finished_level(self):
        project, floor, _ = self.project()
        floor.levels.append(LevelDatum(name="Чистый пол", kind="finished_floor", elevation_mm=30))
        floor.elevation_mm = -3000
        floor.origin = Point2(x=120, y=-90)
        scene = build_finish_presentation(project)
        self.assertEqual(len(scene["slabs"]), 1)
        slab = scene["slabs"][0]
        self.assertEqual(slab["bottom_mm"], -3170)
        self.assertEqual(slab["top_mm"], -2970)
        shape = Polygon(slab["footprint_mm"], slab["holes_mm"])
        self.assertTrue(shape.contains(Point(2120, 910)))
        self.assertFalse(shape.contains(Point(6000, 900)))
        self.assertAlmostEqual(sum(Polygon(t).area for t in slab["cap_triangles_mm"]), shape.area)

    def test_concave_separate_wings_and_shaft_are_not_filled_with_bounding_box(self):
        project, _, _ = fixture(); project.properties = {}
        floor = Floor(name="Сложный этаж")
        project.buildings[0].floors = [floor]
        room = add_polygon_space(floor, code="1", name="Г-образная", points=[Point2(x=x, y=y)
            for x, y in [(0, 0), (4000, 0), (4000, 1000), (1000, 1000), (1000, 4000), (0, 4000)]])
        add_rectangle(floor, code="2", name="Отдельный корпус", x=8000, y=0, width_mm=2000, depth_mm=2000)
        floor.obstacles.append(Obstacle(name="Шахта", kind="shaft", height_mm=3000,
            footprint=[Point2(x=x, y=y) for x, y in [(300, 300), (700, 300), (700, 700), (300, 700)]]))
        scene = build_finish_presentation(project)
        shape = unary_union([Polygon(s["footprint_mm"], s["holes_mm"]) for s in scene["slabs"]])
        for x, y in ((2000, 2000), (6000, 1000), (500, 500)):
            self.assertFalse(shape.contains(Point(x, y)))
        self.assertTrue(shape.contains(Point(9000, 1000)))
        room_scene = build_finish_presentation(project, {"kind": "space", "id": room.id})
        self.assertTrue(room_scene["slabs"])
        self.assertTrue(all(max(x for x, _ in s["footprint_mm"]) < 5000 for s in room_scene["slabs"]))

    def test_two_floors_align_with_panels_and_independent_colors_persist(self):
        project, floor, _ = self.project()
        second, upper, _ = self.project()
        upper.origin = Point2(x=10000, y=15000); upper.elevation_mm = 3000
        project.buildings[0].floors.append(upper)
        project.properties["wall_mounting"]["walls"] += second.properties["wall_mounting"]["walls"]
        signature = input_signature(project)
        face = floor.walls[0].faces[0].id
        project.properties["finish_presentation"] = {"profile_color": "#123456", "face_profile_colors": {face: "#ededed"},
            "floor_color": "#886644", "floor_colors": {upper.id: "#909090"}, "floor_thickness_mm": 180}
        scene = build_finish_presentation(project)
        slabs = {s["floor_id"]: s for s in scene["slabs"]}
        self.assertEqual(set(slabs), {floor.id, upper.id})
        self.assertTrue(Polygon(slabs[floor.id]["footprint_mm"]).equals(Polygon(slabs[upper.id]["footprint_mm"])))
        self.assertEqual(slabs[upper.id]["top_mm"], 3000)
        self.assertEqual(slabs[upper.id]["color"], "#909090")
        self.assertEqual(slabs[floor.id]["color"], "#886644")
        for p in scene["profiles"]:
            self.assertEqual(p["color"], "#ededed" if p["face_id"] == face else "#123456")
        self.assertEqual(signature, input_signature(project))
        self.assertEqual(build_finish_presentation(project.model_validate_json(project.model_dump_json())), scene)

    def test_invalid_color_and_depth_rejected(self):
        for data in ({"profile_color": "transparent"}, {"floor_colors": {"f": "#fff"}}, {"floor_thickness_mm": 0}):
            with self.assertRaises(ValueError):
                Appearance.model_validate(data)


if __name__ == "__main__":
    unittest.main()
