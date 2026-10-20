# Jayson integration

Upstream author: JaysonBilek. Reviewed commits 40f8a46 and 8cfd071, 7 October 2026, on OmniverseExtension.

Source: https://github.com/JosephCapstone/Capstone-Project-P004021Eng/blob/8cfd0717adf3e042b9593e8408e44010d8c69740/OmniverseExtension/DELTA/DELTA/scripts/calloutBox.py

Original scripts are saved in DELTA_APP/references/Jayson-2026-10-07.

The active ClusterBoxCallout in damage_view.py adapts the world-bound-to-viewport projection, label tracking and leader-line approach. It draws the selected cluster box only, using its real box corners, and labels it with the cluster ID. Other boxes are faded by ClusterHighlight. No centroid rectangle is drawn.

Adaptations: dynamic viewport aspect ratio; selection-aware updates; guard against points behind the camera; viewport clipping checks; deferred overlay creation; working subscription/overlay cleanup. Unused PhysX dependencies and constant debug warnings are omitted. Camera navigation remains the existing operator-controlled implementation.

The original eight-stage processing pipeline remains unchanged.

DELTA exposes Free, Top, Front and Right views in a compact View dropdown. Presets focus on the selected cluster or fit the scene when none is selected; camera changes stay in the session layer. Free restores the standard navigable perspective camera.


## Version 07 merge

The version 06 callout is retained, with labels changed from Cluster to Site. Camera switching now authors the render-product camera relationship in the session layer before activating the camera; this prevents Hydra from editing that relationship in the scan root layer. Box shading retains Callum's exported colours and the retained viewer opacity settings.
