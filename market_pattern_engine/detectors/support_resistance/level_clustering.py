from __future__ import annotations

from typing import Any


def cluster_levels(pivots: list[dict[str, Any]], tolerance: float) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for pivot in sorted(pivots, key=lambda item: float(item["price"])):
        price = float(pivot["price"])
        for cluster in clusters:
            center = sum(float(item["price"]) for item in cluster) / len(cluster)
            if abs(price - center) <= tolerance:
                cluster.append(pivot)
                break
        else:
            clusters.append([pivot])
    return clusters
