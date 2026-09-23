#!/usr/bin/env python3
"""
Stage 7: Surface - fields only
=================================
Defines Stage 7's fields, built field-for-field the same way
open_surface_dialog() builds them in pipeline_applet.py. A diff-bound
stage (pipeline.kind == "diff"), same group as Stages 5-6.

Simpler than Stages 5-6: no helper button calls into pipeline_core.py
directly, and unlike Stage 1's Backend choice, Poisson/Ball Pivoting
fields are never hidden based on the radio choice - both sets of
fields just stay visible with "[Poisson]"/"[Ball Pivoting]" label
prefixes, matching the source dialog exactly.

Also the simplest _build_run() so far: report is a plain string, not a
deferred callable - surface_reconstruction.py writes directly to the
requested Output path, no CloudCompare-style renaming or sidecar
reading needed afterward, so there's nothing to resolve post-run the
way Stages 3-6 all needed.

Run standalone:
    pip install PySide6
    python stage7_surface_dialog_qt.py
"""
import sys

from PySide6.QtWidgets import QApplication

from qt_stage_base import QStageDialog, QStagePanel, SCRIPTS_DIR

try:
    import project_manager as pm
except ImportError:
    pm = None

try:
    import pipeline_core as core
except ImportError:
    core = None


class Stage7SurfaceFieldsMixin:

    def _build_surface_fields(self, pipeline=None):
        self.add_pipeline_label(pipeline)

        default_script = str(SCRIPTS_DIR / "surface_reconstruction.py")
        self.add_file_field(
            "script", "Surface reconstruction script (.py):", [("Python files", "*.py")],
            default=default_script)
        self.add_file_field("input", "Classified change cloud .ply:", [("PLY files", "*.ply")])
        self.add_project_picker_button(
            "input", lambda: pm.list_eligible_inputs(self.pipeline, "surface"))

        output_default = self.resolve_project_output_default(pipeline, "surface", ".ply")
        self.add_save_field("output", "Output mesh .ply:", default_ext=".ply",
                             default=output_default)
        self.register_auto_default(
            "output", lambda p: self.resolve_project_output_default(p, "surface", ".ply"))

        self.add_radio_choice("method", "Method:", [
            ("Poisson - smooth continuous surface, good for room shells/walls/floors",
             "poisson"),
            ("Ball Pivoting - stays closer to real points, better for cluttered/mechanical "
             "detail", "ball_pivoting"),
        ], hint=(
            "Poisson tends to over-smooth cluttered/complex scenes into blobs, since it "
            "fits one continuous surface through data that isn't actually one continuous "
            "surface. Ball Pivoting stays much closer to the actual point positions, at "
            "the cost of more holes where point density is uneven. Which suits a given "
            "change region is still an open question worth checking visually after each "
            "run."))

        self.add_text_field("depth", "[Poisson] Octree depth:", default="9")
        self.add_text_field(
            "density_trim_percentile", "[Poisson] Density trim percentile:", default="10")
        self.add_hint(
            "[Poisson only] Depth: higher = more detail but slower and more prone to "
            "noise/artifacts; 8-10 is a reasonable range. Density trim: percentile of "
            "lowest-density vertices removed as likely reconstruction artifacts (0 disables "
            "trimming and keeps the mesh watertight, at the cost of keeping spurious blobby "
            "geometry away from the real data).")
        self.add_text_field(
            "ball_radii", "[Ball Pivoting] Ball radii (comma-separated, optional):",
            default="")
        self.add_hint(
            "[Ball Pivoting only] e.g. '0.02,0.04,0.08', in the same units as the point "
            "cloud. Leave blank to auto-estimate from the cloud's own point spacing.")
        self.add_text_field(
            "carry_field", "Carry field (optional, e.g. 'scalar_M3C2_distance'):",
            default="scalar_M3C2_distance")
        self.add_hint(
            "Carries this per-vertex scalar field from the input onto the reconstructed "
            "mesh's vertices (via nearest-original-point lookup), so the mesh can still be "
            "colored by change magnitude in USD. Requires scipy on the machine running the "
            "script. Leave blank to skip. The field's actual on-disk name depends on which "
            "stage last saved the file - Stage 6 (Classify) re-saving via Open3D typically "
            "produces 'scalar_M3C2_distance' (confirmed in testing), not the plain "
            "'M3C2 distance' name CloudCompare itself uses internally. If reconstruction "
            "fails with a 'field not found' error, that error lists every actual field "
            "name available in the file - use one of those.")

        self.form.addStretch(1)

    def _build_run(self):
        """Real Run - matches open_surface_dialog()'s build() in
        pipeline_applet.py exactly. report is a plain string, not a
        deferred callable - see module docstring for why this is the
        simplest _build_run() built so far."""
        script = self.require("script", "Surface reconstruction script")
        input_ply = self.require("input", "Classified change cloud .ply")
        output = self.require("output", "Output mesh .ply")
        # Radio-button-backed (add_radio_choice) - always exactly
        # "poisson" or "ball_pivoting", never blank/typo'd, so no
        # fallback is needed here.
        method = self.fields["method"].get()
        depth = None
        density_trim_percentile = None
        if method == "poisson":
            try:
                depth = int(self.fields["depth"].get())
                density_trim_percentile = float(self.fields["density_trim_percentile"].get())
            except ValueError:
                raise ValueError("Octree depth must be a whole number, density trim "
                                  "percentile must be a number.")
        ball_radii = self.fields["ball_radii"].get().strip() or None
        carry_field = self.fields["carry_field"].get().strip() or None

        active_pipeline = self.get_active_pipeline_for_run()
        cmd = core.build_surface_command(
            script, input_ply, output, method=method, depth=depth,
            density_trim_percentile=density_trim_percentile, ball_radii=ball_radii,
            carry_field=carry_field, pipeline=active_pipeline)
        finish_info = {"pipeline": active_pipeline, "stage_name": "surface", "output": output}

        report = (
            "=== SUMMARY ===\n"
            f"Input: {input_ply}\n"
            f"Method: {method}\n"
            + (f"Octree depth: {depth}, density trim: {density_trim_percentile}%\n"
               if method == "poisson" else
               f"Ball radii: {ball_radii or 'auto-estimated'}\n")
            + (f"Carrying field: {carry_field}\n" if carry_field else
               "No field carried through.\n")
            + f"Saved to: {output}\n\n"
            "=== NOTE ===\n"
            "Check the tool output above for vertex/triangle counts and whether the "
            "mesh came out watertight. 0 triangles means reconstruction likely failed "
            "- check the input cloud has enough points and reasonable density.\n\n"
            "=== NEXT STEPS ===\n"
            "Open the mesh in CloudCompare or Omniverse to check it visually, then "
            "use it as the --change input to Stage 8 (Export)."
        )
        return cmd, report, finish_info


class Stage7SurfaceDialog(QStageDialog, Stage7SurfaceFieldsMixin):
    """Standalone popup - run this file directly to open just this
    dialog."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__("Stage 7: Surface", parent, on_output=on_output, on_status=on_status)
        self._build_surface_fields(pipeline=pipeline)


class Stage7SurfacePanel(QStagePanel, Stage7SurfaceFieldsMixin):
    """Embeddable panel - used by pipeline_applet_qt_template.py as a
    page of stageStack."""

    def __init__(self, parent=None, pipeline=None, on_output=None, on_status=None):
        super().__init__(parent, on_output=on_output, on_status=on_status)
        self._build_surface_fields(pipeline=pipeline)


def main():
    app = QApplication(sys.argv)
    dlg = Stage7SurfaceDialog(pipeline=None)
    dlg.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
