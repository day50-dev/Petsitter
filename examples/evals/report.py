def print_comparison(results: list[dict]) -> None:
    if not results:
        print("No results to report.")
        return

    headers = ["Scenario", "Raw", "Petsitter", "Delta"]
    rows = []
    for r in results:
        raw = r["raw_score"]
        pet = r["petsitter_score"]
        delta = pet - raw
        rows.append([r["name"], f"{raw:.0%}", f"{pet:.0%}", f"{delta:+.0%}"])

    col_widths = []
    for i in range(len(headers)):
        data_max = max(len(str(row[i])) for row in rows) if rows else 0
        col_widths.append(max(len(headers[i]), data_max) + 2)

    sep = "+" + "+".join("-" * w for w in col_widths) + "+"

    print(sep)
    header_cells = "|".join(h.center(w) for h, w in zip(headers, col_widths))
    print(f"|{header_cells}|")
    print("|" + "|".join("=" * w for w in col_widths) + "|")
    for row in rows:
        cells = []
        for i, (v, w) in enumerate(zip(row, col_widths)):
            s = str(v).rjust(w - 1) if i > 0 else str(v).ljust(w - 1)
            cells.append(f" {s}")
        print("|" + "|".join(cells) + " |")
    print(sep)

    avg_raw = sum(r["raw_score"] for r in results) / len(results)
    avg_pet = sum(r["petsitter_score"] for r in results) / len(results)
    print(f"\nAverage Raw Score:       {avg_raw:.1%}")
    print(f"Average Petsitter Score: {avg_pet:.1%}")
    print(f"Overall Improvement:     {avg_pet - avg_raw:+.1%}")


def print_detailed(results: list[dict]) -> None:
    for r in results:
        print(f"\n{'='*60}")
        print(f"Scenario: {r['name']}")
        print(f"Raw score:       {r['raw_score']:.0%}")
        print(f"Petsitter score: {r['petsitter_score']:.0%}")
        print(f"Delta:           {r['delta']:+.0%}")

        raw_msg = r.get("raw_response", {}).get("choices", [{}])[0].get("message", {})
        pet_msg = r.get("pet_response", {}).get("choices", [{}])[0].get("message", {})

        raw_content = raw_msg.get("content", "") or str(raw_msg.get("tool_calls", ""))
        pet_content = pet_msg.get("content", "") or str(pet_msg.get("tool_calls", ""))

        max_preview = 300
        print(f"\n  Raw output:      {raw_content[:max_preview]}")
        print(f"  Petsitter output: {pet_content[:max_preview]}")
