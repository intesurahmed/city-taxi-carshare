"""Batch experiments for the City Taxi Carshare simulation.

Runs the four incremental stages of the project guide on many random cities (seeds)
and several fleet sizes, using Mesa's batch_run, then writes:

  results_raw.csv       one row per run (stage x fleet size x seed)
  results_summary.csv   mean and 95% confidence interval per stage and fleet size
  results_summary.md    the same summary as a Markdown table, ready for the report
  results_figure.png    fuel per passenger and passengers lost, per stage

Usage:  python experiments.py            (30 seeds, about one minute)
        python experiments.py --seeds 10 (quicker test)
"""

import argparse  # Parse the optional command-line seed count.
import math  # Provide square-root calculations for confidence intervals.

import matplotlib  # Configure Matplotlib before importing pyplot.
matplotlib.use("Agg")  # Draw figures to files without opening a window.
import matplotlib.pyplot as plt  # Create and save experiment charts.
import pandas as pd  # Store and aggregate batch results in DataFrames.
from mesa.batchrunner import batch_run  # Run many Mesa simulations automatically.

from app import CityTaxiModel  # Import the model under experiment.

# Define the four incremental project stages as model parameters.
STAGES = {  # Map each report label to its simulation configuration.
    "1 Solo": dict(taxi_capacity=1, allow_route_change=True),  # Run one passenger per taxi.
    "2 Share 2, no detour": dict(taxi_capacity=2, allow_route_change=False),  # Share two seats without route changes.
    "3 Share 2, multi-hop": dict(taxi_capacity=2, allow_route_change=True),  # Share two seats with route changes.
    "4 Share 3, multi-hop": dict(taxi_capacity=3, allow_route_change=True),  # Share three seats with route changes.
}  # Finish the stage definitions.
FLEETS = [2, 4, 8]  # Compare undersized, tight, and comfortable fleets for 25 passengers.
PASSENGERS = 25  # Keep the passenger population consistent across experiments.
MAX_STEPS = 150  # Allow enough time for delivery or passenger abandonment.


def run_all(n_seeds):  # Run every stage, fleet size, and random seed combination.
    rows = []  # Collect one result dictionary for each completed simulation.
    seeds = list(range(1, n_seeds + 1))  # Create reproducible seed values starting at one.
    for stage, params in STAGES.items():  # Run each experimental stage separately.
        results = batch_run(  # Execute the model across all configured combinations.
            CityTaxiModel,  # Supply the Mesa model class to the batch runner.
            parameters=dict(num_taxis=FLEETS, num_passengers=PASSENGERS, verbose=False, **params),  # Vary fleet size and stage parameters.
            rng=seeds,  # Reuse seeds in every stage so cities are paired and comparable.
            max_steps=MAX_STEPS,  # Stop each run after the configured maximum number of steps.
            data_collection_period=-1,  # Keep only the final state of each run.
            display_progress=False,  # Avoid progress output during batch execution.
        )  # Finish the batch run call.
        for r in results:  # Label every result with the stage that produced it.
            r["Stage"] = stage  # Store the human-readable stage name.
        rows += results  # Append this stage's runs to the complete result list.
    df = pd.DataFrame(rows)  # Convert all result dictionaries into a table.
    df["Gave Up"] = df["Impatient (Walked)"]  # Use a short report-friendly name for abandoned passengers.
    df["Door to Door"] = df["Avg Wait Time"] + df["Avg Travel Time"]  # Combine waiting and riding time.
    return df  # Return the complete raw experiment table.


def add_paired_savings(df):  # Add fuel comparisons against matching solo runs.
    """Fuel saving against the solo run of the SAME city (same seed, same fleet).

    This replaces the old fixed 'direct + 4' estimate with a measured baseline."""
    solo = (df[df["Stage"] == "1 Solo"]  # Keep only the solo baseline runs.
            .set_index(["num_taxis", "seed"])[["Fuel per Arrival", "Total Fuel / Pollution (Distance)"]]  # Match by fleet and city seed.
            .rename(columns={"Fuel per Arrival": "Solo FpA", "Total Fuel / Pollution (Distance)": "Solo Fuel"}))  # Give baseline columns distinct names.
    df = df.join(solo, on=["num_taxis", "seed"])  # Attach each matching solo baseline to every run.
    df["Saving vs Solo (%)"] = 100 * (1 - df["Fuel per Arrival"] / df["Solo FpA"])  # Calculate percentage fuel savings.
        # Measure the error of the older fixed baseline estimate on each run.
    df["Estimate Error (%)"] = 100 * (df["Est. Solo Fuel (No Carshare)"] / df["Total Fuel / Pollution (Distance)"] - 1)  # Compare estimated and measured fuel.
    return df  # Return the table with paired comparison columns.


def ci95(series):  # Calculate the half-width of a 95% confidence interval.
    """Half-width of the 95% confidence interval of the mean (normal approximation)."""
    n = series.count()  # Count non-missing observations.
    return 1.96 * series.std(ddof=1) / math.sqrt(n) if n > 1 else float("nan")  # Use the normal approximation when enough data exists.


def summarise(df):  # Aggregate raw runs by fleet size and experimental stage.
    metrics = ["Arrived", "Gave Up", "Avg Wait Time", "Avg Travel Time", "Door to Door",  # Select outcome and time metrics.
               "Total Fuel / Pollution (Distance)", "Fuel per Arrival", "Saving vs Solo (%)"]  # Select fuel and comparison metrics.
    grouped = df.groupby(["num_taxis", "Stage"])  # Create one group for each fleet-stage pair.
    summary = pd.concat({m: grouped[m].agg(["mean", ci95]) for m in metrics}, axis=1)  # Calculate means and confidence intervals.
    summary.columns = [f"{m} {stat}" for m, stat in summary.columns]  # Flatten the resulting multi-level column names.
    return summary.reset_index()  # Return fleet and stage as ordinary columns.


def to_markdown(summary):  # Convert the summary DataFrame into a report-ready Markdown table.
    short = {"Arrived": "Arrived", "Gave Up": "Gave up", "Avg Wait Time": "Wait",  # Map source metrics to short headers.
             "Avg Travel Time": "Travel", "Door to Door": "Door-to-door",  # Add time-related headers.
             "Fuel per Arrival": "Fuel / passenger", "Saving vs Solo (%)": "Saving vs solo (%)"}  # Add fuel-related headers.
    lines = ["| Taxis | Stage | " + " | ".join(short.values()) + " |",  # Build the Markdown header row.
             "| --- | --- | " + " | ".join("---" for _ in short) + " |"]  # Build the Markdown separator row.
    for _, r in summary.iterrows():  # Format each fleet-stage summary row.
        cells = []  # Collect formatted metric cells for the current row.
        for m in short:  # Process each selected metric.
            mean, ci = r[f"{m} mean"], r[f"{m} ci95"]  # Read the mean and interval values.
            cells.append("—" if pd.isna(ci) or (m.startswith("Saving") and r["Stage"] == "1 Solo")  # Hide unavailable intervals and solo savings.
                         else f"{mean:.1f} ± {ci:.1f}")  # Format available values to one decimal place.
        lines.append(f"| {r['num_taxis']} | {r['Stage']} | " + " | ".join(cells) + " |")  # Append the formatted row.
    return "\n".join(lines)  # Return the complete Markdown document.


def draw_figure(summary, path):  # Draw the two report charts and save them to disk.
    stages = list(STAGES)  # Preserve the configured stage order in the chart.
    # Use grey for the solo reference and progressively darker blue for sharing stages.
    colors = ["#9aa0a6", "#a9c6ec", "#5b8fd6", "#1f5fb4"]  # Define one color per stage.
    fig, axes = plt.subplots(1, 2, figsize=(12, 4.6))  # Create fuel and passenger-loss panels.
    width = 0.2  # Set the width of each grouped bar.
    for ax, metric, title, unit in [  # Configure the metric represented by each panel.
        (axes[0], "Fuel per Arrival", "Fuel per delivered passenger", "districts driven per passenger"),  # Configure the fuel panel.
        (axes[1], "Gave Up", "Passengers lost (gave up and walked)", "passengers out of 25"),  # Configure the loss panel.
    ]:  # Finish the panel definitions.
        for i, stage in enumerate(stages):  # Draw one bar group for each stage.
            part = summary[summary["Stage"] == stage].set_index("num_taxis").loc[FLEETS]  # Select this stage's fleet-size rows.
            xs = [f + (i - 1.5) * width for f in range(len(FLEETS))]  # Offset bars within each fleet group.
            ax.bar(xs, part[f"{metric} mean"], width - 0.02, yerr=part[f"{metric} ci95"],  # Plot means with confidence intervals.
                   color=colors[i], label=stage, capsize=3, error_kw=dict(lw=1, ecolor="#444"))  # Style the current stage bars.
        ax.set_xticks(range(len(FLEETS)), [f"{f} taxis" for f in FLEETS])  # Label each fleet-size group.
        ax.set_title(title, loc="left", fontsize=12, fontweight="bold")  # Add the panel title.
        ax.set_ylabel(unit)  # Label the panel's measurement unit.
        ax.grid(axis="y", color="#e5e5e5")  # Add horizontal guide lines.
        ax.set_axisbelow(True)  # Place grid lines behind the bars.
        for side in ("top", "right"):  # Remove the two unnecessary chart borders.
            ax.spines[side].set_visible(False)  # Hide the selected border.
    axes[0].legend(frameon=False, fontsize=9)  # Explain stage colors using the first panel.
    fig.suptitle(f"Mean of {summary_seeds} cities per bar, error bars = 95% confidence interval",  # Add the shared chart subtitle.
                 x=0.01, ha="left", fontsize=9, color="#555")  # Position and style the subtitle.
    fig.tight_layout(rect=(0, 0, 1, 0.95))  # Reserve room for the subtitle.
    fig.savefig(path, dpi=160)  # Save the finished figure at report-friendly resolution.
    plt.close(fig)  # Release the figure resources.


if __name__ == "__main__":  # Run experiments only when this file is executed directly.
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)  # Create the command-line parser.
    parser.add_argument("--seeds", type=int, default=30, help="number of random cities per configuration")  # Add the seed-count option.
    args = parser.parse_args()  # Read command-line arguments.
    summary_seeds = args.seeds  # Make the seed count available to the chart subtitle.

    df = add_paired_savings(run_all(args.seeds))  # Run simulations and add paired baseline metrics.
    df.to_csv("results_raw.csv", index=False)  # Save one row per simulation run.

    summary = summarise(df)  # Calculate grouped means and confidence intervals.
    summary.to_csv("results_summary.csv", index=False)  # Save the numerical summary as CSV.
    with open("results_summary.md", "w") as f:  # Open the Markdown report for writing.
        f.write(f"Mean ± 95% CI over {args.seeds} seeds, {PASSENGERS} passengers, run to completion.\n\n")  # Write the report description.
        f.write(to_markdown(summary) + "\n")  # Write the formatted summary table.
    draw_figure(summary, "results_figure.png")  # Save the visual summary chart.

    unresolved = int(df["Unresolved"].sum())  # Count passengers still unresolved at the end of all runs.
    solo_err = df[df["Stage"] == "1 Solo"].groupby("num_taxis")["Estimate Error (%)"].mean()  # Average old-baseline error by solo fleet size.
    print(to_markdown(summary))  # Print the Markdown summary to the terminal.
    print(f"\nRuns: {len(df)} | passengers still unresolved at the end: {unresolved}")  # Print run and unresolved counts.
    print("Old 'direct + 4' baseline error on solo runs (%):", solo_err.round(1).to_dict())  # Print rounded baseline errors.
