#!/usr/bin/env python3
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


@dataclass(frozen=True)
class PlotTheme:
    bg: str = "#0B0B0F"
    card: str = "#14141C"
    ink: str = "#F5F5FA"
    muted: str = "#B4B4C8"
    muted2: str = "#46465A"

    cyan: str = "#00D4FF"
    purple: str = "#A855F7"
    pink: str = "#FF5D9E"
    amber: str = "#F59E0B"
    green: str = "#22C55E"


@dataclass(frozen=True)
class RunPaths:
    run_dir: Path

    @property
    def csv_dir(self) -> Path:
        return self.run_dir / "csv"


@dataclass(frozen=True)
class PlotOutputs:
    out_dir: Path

    def path(self, name: str) -> Path:
        return self.out_dir / f"{name}.png"

    @property
    def metrics_path(self) -> Path:
        return self.out_dir / "metrics.json"


@dataclass(frozen=True)
class DataBundle:
    discovery_inclusions: pd.DataFrame
    provider_stats: pd.DataFrame
    feed_panel: pd.DataFrame
    feed_items: pd.DataFrame
    authors: pd.DataFrame
    post_labels: pd.DataFrame


EMU_PER_INCH = 914400


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _apply_style(theme: PlotTheme) -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": theme.bg,
            "axes.facecolor": theme.bg,
            "savefig.facecolor": theme.bg,
            "text.color": theme.ink,
            "axes.labelcolor": theme.ink,
            "axes.edgecolor": theme.muted2,
            "xtick.color": theme.muted,
            "ytick.color": theme.muted,
            "font.size": 13,
            "axes.titlesize": 15,
            "axes.titleweight": "bold",
            "axes.grid": True,
            "grid.color": theme.muted2,
            "grid.alpha": 0.35,
            "grid.linestyle": "-",
        }
    )


def _finish(fig: plt.Figure, out_path: Path, *, dpi: int = 220) -> None:
    _ensure_dir(out_path.parent)
    fig.tight_layout(pad=1.2)
    fig.savefig(out_path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


def _gini(values: np.ndarray) -> float:
    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    vals = vals[vals >= 0]
    if vals.size == 0:
        return float("nan")
    if np.all(vals == 0):
        return 0.0
    vals = np.sort(vals)
    n = vals.size
    cum = np.cumsum(vals)
    return float((n + 1 - 2 * np.sum(cum) / cum[-1]) / n)


def _lorenz(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    vals = vals[vals >= 0]
    if vals.size == 0:
        return np.array([0.0, 1.0]), np.array([0.0, 1.0])
    vals = np.sort(vals)
    cum = np.cumsum(vals)
    total = cum[-1]
    if total <= 0:
        return np.array([0.0, 1.0]), np.array([0.0, 1.0])
    y = np.concatenate([[0.0], cum / total])
    x = np.linspace(0.0, 1.0, y.size)
    return x, y


def _top_share(values: np.ndarray, frac: float) -> float:
    if not (0 < frac <= 1):
        raise ValueError(f"frac must be in (0,1], got {frac}")
    vals = np.asarray(values, dtype=float)
    vals = vals[np.isfinite(vals)]
    vals = vals[vals >= 0]
    if vals.size == 0:
        return float("nan")
    if np.all(vals == 0):
        return 0.0
    vals = np.sort(vals)[::-1]
    k = max(1, int(np.ceil(frac * vals.size)))
    return float(vals[:k].sum() / vals.sum())


def load_data(paths: RunPaths) -> DataBundle:
    discovery_inclusions = pd.read_csv(paths.csv_dir / "discovery_feed_inclusions.csv")
    provider_stats = pd.read_csv(paths.csv_dir / "provider_stats.csv")
    feed_panel = pd.read_csv(paths.csv_dir / "feed_panel.csv", usecols=["feed_uri", "feed_group", "provider_bucket", "display_name"])

    feed_items = pd.read_csv(
        paths.csv_dir / "feed_items.csv.gz",
        usecols=["feed_uri", "feed_group", "rank", "post_uri", "post_cid", "author_did"],
    )

    authors = pd.read_csv(paths.csv_dir / "authors.csv.gz", usecols=["author_did", "followers_count"])

    post_labels = pd.read_csv(paths.csv_dir / "post_labels.csv.gz", usecols=["feed_uri", "post_uri", "post_cid", "label_val"])

    return DataBundle(
        discovery_inclusions=discovery_inclusions,
        provider_stats=provider_stats,
        feed_panel=feed_panel,
        feed_items=feed_items,
        authors=authors,
        post_labels=post_labels,
    )


def plot_h1(bundle: DataBundle, outputs: PlotOutputs, theme: PlotTheme, *, size_in: tuple[float, float]) -> dict[str, float]:
    slots = bundle.discovery_inclusions["slot_count"].to_numpy(dtype=float)
    gini = _gini(slots)
    top_1pct = _top_share(slots, 0.01)
    top_10pct = _top_share(slots, 0.10)
    x, y = _lorenz(slots)

    fig, ax = plt.subplots(figsize=size_in)
    ax.plot(x, y, color=theme.pink, linewidth=3, label="Observed")
    ax.plot([0, 1], [0, 1], color=theme.muted2, linewidth=2, linestyle="--", label="Equal share")
    ax.set_xlabel("Fraction of feeds (sorted)")
    ax.set_ylabel("Fraction of starter-pack slots")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(loc="lower right", frameon=False, fontsize=10)
    ax.text(
        0.03,
        0.92,
        f"Gini {gini:.2f}\nTop 1%: {top_1pct*100:.0f}%\nTop 10%: {top_10pct*100:.0f}%",
        transform=ax.transAxes,
        fontsize=11,
        color=theme.ink,
        bbox={"boxstyle": "round,pad=0.35", "facecolor": theme.card, "edgecolor": theme.muted2, "alpha": 0.9},
    )
    _finish(fig, outputs.path("h1_discovery_lorenz"))
    return {"h1_gini": gini, "h1_top_1pct_share": top_1pct, "h1_top_10pct_share": top_10pct}


def plot_h2(bundle: DataBundle, outputs: PlotOutputs, theme: PlotTheme, *, size_in: tuple[float, float], top_n: int = 10) -> dict[str, float | str]:
    df = bundle.provider_stats.sort_values("discovery_share", ascending=False).head(top_n).copy()
    df["provider_short"] = (
        df["provider_bucket"]
        .astype(str)
        .str.replace("did:web:", "", regex=False)
        .str.replace("plc_bucket:", "plc:", regex=False)
    )

    y = np.arange(len(df))[::-1]
    fig, ax = plt.subplots(figsize=size_in)
    ax.barh(y + 0.18, df["discovery_share"].to_numpy() * 100, height=0.32, color=theme.cyan, label="Discovery share")
    ax.barh(y - 0.18, df["hosting_share"].to_numpy() * 100, height=0.32, color=theme.muted2, label="Hosting share")
    ax.set_yticks(y, labels=df["provider_short"].to_list())
    ax.set_xlabel("Share (%)")
    ax.legend(loc="lower right", frameon=False, fontsize=10)
    max_share = float(df[["discovery_share", "hosting_share"]].to_numpy().max() * 100)
    ax.set_xlim(0, max(1.0, max_share * 1.25))

    # Call out the most asymmetric provider (leverage = discovery_share / hosting_share).
    finite = df[np.isfinite(df["leverage_ratio"])].copy()
    max_lr_row = finite.sort_values("leverage_ratio", ascending=False).head(1).iloc[0] if not finite.empty else None
    if max_lr_row is not None:
        ax.text(
            0.03,
            0.92,
            (
                "Largest leverage:\n"
                f"{max_lr_row['provider_short']}\n"
                f"{max_lr_row['discovery_share']*100:.1f}% discovery vs {max_lr_row['hosting_share']*100:.2f}% hosting\n"
                f"(×{max_lr_row['leverage_ratio']:.0f})"
            ),
            transform=ax.transAxes,
            fontsize=10.5,
            color=theme.ink,
            bbox={"boxstyle": "round,pad=0.35", "facecolor": theme.card, "edgecolor": theme.muted2, "alpha": 0.9},
        )

    top = df.iloc[0]
    _finish(fig, outputs.path("h2_provider_leverage"))
    return {
        "h2_top_provider": str(top["provider_short"]),
        "h2_top_provider_hosting_share": float(top["hosting_share"]),
        "h2_top_provider_discovery_share": float(top["discovery_share"]),
        "h2_top_provider_leverage_ratio": float(top["leverage_ratio"]),
        "h2_max_leverage_provider": (str(max_lr_row["provider_short"]) if max_lr_row is not None else ""),
        "h2_max_leverage_ratio": (float(max_lr_row["leverage_ratio"]) if max_lr_row is not None else float("nan")),
    }


def plot_h3(bundle: DataBundle, outputs: PlotOutputs, theme: PlotTheme, *, size_in: tuple[float, float]) -> dict[str, float]:
    counts = bundle.feed_items["author_did"].value_counts().to_numpy(dtype=float)
    gini = _gini(counts)
    top_1pct = _top_share(counts, 0.01)
    top_10pct = _top_share(counts, 0.10)
    x, y = _lorenz(counts)

    fig, ax = plt.subplots(figsize=size_in)
    ax.plot(x, y, color=theme.purple, linewidth=3, label="Observed")
    ax.plot([0, 1], [0, 1], color=theme.muted2, linewidth=2, linestyle="--", label="Equal share")
    ax.set_xlabel("Fraction of authors (sorted)")
    ax.set_ylabel("Fraction of impressions")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(loc="lower right", frameon=False, fontsize=10)
    ax.text(
        0.03,
        0.92,
        f"Gini {gini:.2f}\nTop 1%: {top_1pct*100:.0f}%\nTop 10%: {top_10pct*100:.0f}%",
        transform=ax.transAxes,
        fontsize=11,
        color=theme.ink,
        bbox={"boxstyle": "round,pad=0.35", "facecolor": theme.card, "edgecolor": theme.muted2, "alpha": 0.9},
    )
    _finish(fig, outputs.path("h3_exposure_lorenz"))
    return {"h3_gini": gini, "h3_top_1pct_share": top_1pct, "h3_top_10pct_share": top_10pct}


def plot_h4(bundle: DataBundle, outputs: PlotOutputs, theme: PlotTheme, *, size_in: tuple[float, float]) -> dict[str, float]:
    # Post-level overlap: how often the same post appears across multiple feeds.
    # Each row in feed_items is one ranked "impression" slot (feed_uri × rank).
    df = bundle.feed_items[["feed_uri", "post_uri", "post_cid"]].copy()
    post_feed_counts = df.groupby(["post_uri", "post_cid"])["feed_uri"].nunique().rename("n_feeds").reset_index()
    merged = df.merge(post_feed_counts, on=["post_uri", "post_cid"], how="left")

    share_ge2 = float((merged["n_feeds"] >= 2).mean())
    share_ge3 = float((merged["n_feeds"] >= 3).mean())

    bins = [1, 2, 3, 6, 11, 21, 51, 101, 1_000_000]
    labels = ["1", "2", "3-5", "6-10", "11-20", "21-50", "51-100", "101+"]
    merged["bin"] = pd.cut(merged["n_feeds"], bins=bins, labels=labels, right=False)
    dist = merged["bin"].value_counts(normalize=True).reindex(labels).fillna(0.0)

    fig, ax = plt.subplots(figsize=size_in)
    y = np.arange(len(labels))[::-1]
    ax.barh(y, dist.to_numpy()[::-1] * 100, color=theme.amber, alpha=0.9)
    ax.set_yticks(y, labels=labels[::-1])
    ax.set_xlabel("Impression share (%)")
    ax.set_ylabel("# feeds per post")
    ax.set_xlim(0, max(1.0, float(dist.max() * 100 * 1.25)))
    ax.grid(True, axis="x")
    ax.grid(False, axis="y")

    ax.text(
        0.03,
        0.92,
        f"Multi-feed posts:\n≥2 feeds: {share_ge2*100:.0f}% of impressions\n≥3 feeds: {share_ge3*100:.0f}%",
        transform=ax.transAxes,
        fontsize=10.5,
        color=theme.ink,
        bbox={"boxstyle": "round,pad=0.35", "facecolor": theme.card, "edgecolor": theme.muted2, "alpha": 0.9},
    )

    _finish(fig, outputs.path("h4_overlap_box"))
    return {"h4_share_impressions_post_in_ge2_feeds": share_ge2, "h4_share_impressions_post_in_ge3_feeds": share_ge3}


def plot_h5(bundle: DataBundle, outputs: PlotOutputs, theme: PlotTheme, *, size_in: tuple[float, float]) -> dict[str, float]:
    exposure = bundle.feed_items["author_did"].value_counts().rename_axis("author_did").reset_index(name="impressions")
    df = exposure.merge(bundle.authors, on="author_did", how="left")
    df = df[df["followers_count"].notna()].copy()
    df["followers_count"] = df["followers_count"].astype(float)
    df = df[df["followers_count"] >= 0].copy()

    df["decile"] = pd.qcut(df["followers_count"].rank(method="first"), 10, labels=False)
    dec = df.groupby("decile")["impressions"].sum()
    share = (dec / dec.sum()).to_numpy()

    fig, ax = plt.subplots(figsize=size_in)
    xs = np.arange(10)
    ax.bar(xs, share * 100, color=theme.green, alpha=0.9)
    ax.set_xticks(xs, labels=[f"D{i+1}" for i in xs])
    ax.set_xlabel("Author follower decile (low → high)")
    ax.set_ylabel("Impression share (%)")
    ax.set_ylim(0, max(1.0, float(share.max() * 100 * 1.35)))

    top_share = float(share[-1])
    ax.text(
        0.03,
        0.92,
        f"Top decile gets {top_share*100:.0f}%\n(baseline: 10%)",
        transform=ax.transAxes,
        fontsize=11,
        color=theme.ink,
        bbox={"boxstyle": "round,pad=0.35", "facecolor": theme.card, "edgecolor": theme.muted2, "alpha": 0.9},
    )

    _finish(fig, outputs.path("h5_deciles"))
    return {"h5_top_decile_share": top_share}


def plot_h6(
    bundle: DataBundle, outputs: PlotOutputs, theme: PlotTheme, *, size_in: tuple[float, float], min_impressions: int = 100
) -> dict[str, float]:
    keep = {
        "porn",
        "sexual",
        "nudity",
        "graphic-media",
        "self-harm",
        "intolerant",
        "rude",
        "suggestive",
        "sexual-figurative",
    }
    df_labels = bundle.post_labels[bundle.post_labels["label_val"].isin(keep)].copy()
    df_labels["is_adult"] = df_labels["label_val"].isin({"porn", "sexual", "nudity", "suggestive", "sexual-figurative"})

    denom = bundle.feed_items.groupby("feed_uri").size().rename("impressions").reset_index()
    denom = denom[denom["impressions"] >= min_impressions].copy()

    uniq_adult = df_labels[df_labels["is_adult"]].drop_duplicates(subset=["feed_uri", "post_uri", "post_cid"])
    adult_cnt = uniq_adult.groupby("feed_uri").size().rename("labeled_adult").reset_index()

    uniq_graphic = df_labels[df_labels["label_val"] == "graphic-media"].drop_duplicates(subset=["feed_uri", "post_uri", "post_cid"])
    graphic_cnt = uniq_graphic.groupby("feed_uri").size().rename("labeled_graphic").reset_index()

    df = (
        denom.merge(adult_cnt, on="feed_uri", how="left")
        .merge(graphic_cnt, on="feed_uri", how="left")
        .merge(bundle.feed_panel[["feed_uri", "feed_group"]], on="feed_uri", how="left")
    )
    df = df.fillna({"labeled_adult": 0})
    df = df.fillna({"labeled_graphic": 0})

    df["rate_adult"] = df["labeled_adult"] / df["impressions"]
    df["rate_graphic"] = df["labeled_graphic"] / df["impressions"]

    def _ecdf_line(values: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        vals = np.asarray(values, dtype=float)
        vals = vals[np.isfinite(vals)]
        vals = np.clip(vals, 0, 1)
        vals = np.sort(vals)
        if vals.size == 0:
            return np.array([0.0, 1.0]), np.array([0.0, 1.0])
        x = np.linspace(0.0, 1.0, vals.size)
        return x, vals

    fig, ax = plt.subplots(figsize=size_in)
    x_a, y_a = _ecdf_line(df["rate_adult"].to_numpy())
    x_g, y_g = _ecdf_line(df["rate_graphic"].to_numpy())

    ax.plot(x_a, y_a, color=theme.pink, linewidth=3, label="Adult labels")
    ax.plot(x_g, y_g, color=theme.purple, linewidth=3, label="Graphic media")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.set_xlabel("Fraction of feeds (sorted)")
    ax.set_ylabel("Labeled impression rate")
    ax.legend(loc="lower right", frameon=False, fontsize=9.5)

    share_any_adult = float((df["labeled_adult"] > 0).mean())
    p90 = float(df["rate_adult"].quantile(0.90))
    p95 = float(df["rate_adult"].quantile(0.95))
    p99 = float(df["rate_adult"].quantile(0.99))
    ax.text(
        0.03,
        0.92,
        f"Adult labels (feeds with ≥{min_impressions} impressions):\n"
        f"Any adult: {share_any_adult*100:.0f}% of feeds\n"
        f"P90: {p90*100:.0f}%  P95: {p95*100:.0f}%  P99: {p99*100:.0f}%",
        transform=ax.transAxes,
        fontsize=10.0,
        color=theme.ink,
        bbox={"boxstyle": "round,pad=0.35", "facecolor": theme.card, "edgecolor": theme.muted2, "alpha": 0.9},
    )

    _finish(fig, outputs.path("h6_label_variability"))
    return {
        "h6_min_impressions": float(min_impressions),
        "h6_share_feeds_with_any_adult_labels": share_any_adult,
        "h6_adult_rate_p90": p90,
        "h6_adult_rate_p95": p95,
        "h6_adult_rate_p99": p99,
        "h6_max_adult_rate": float(df["rate_adult"].max()),
        "h6_max_graphic_rate": float(df["rate_graphic"].max()),
    }


def main() -> None:
    theme = PlotTheme()
    _apply_style(theme)

    paths = RunPaths(run_dir=Path("out/bsky_fair_run_20260201T090454Z"))
    outputs = PlotOutputs(out_dir=Path("Slides/Story1/assets/plots"))
    _ensure_dir(outputs.out_dir)

    # Match the right-card aspect ratio in the animated blackboard deck (~3.85 × 4.80 inches).
    size_in = (3.85, 4.80)

    bundle = load_data(paths)

    metrics: dict[str, float | str] = {}
    metrics.update(plot_h1(bundle, outputs, theme, size_in=size_in))
    metrics.update(plot_h2(bundle, outputs, theme, size_in=size_in))
    metrics.update(plot_h3(bundle, outputs, theme, size_in=size_in))
    metrics.update(plot_h4(bundle, outputs, theme, size_in=size_in))
    metrics.update(plot_h5(bundle, outputs, theme, size_in=size_in))
    metrics.update(plot_h6(bundle, outputs, theme, size_in=size_in))

    outputs.metrics_path.write_text(json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote plots → {outputs.out_dir}")
    print(f"Wrote metrics → {outputs.metrics_path}")


if __name__ == "__main__":
    main()
