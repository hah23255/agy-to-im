#!/usr/bin/env python3
"""Graphify & Google OKF (Open Knowledge Format) v0.1 Integration Proof-of-Concept.

This script implements both:
1. An Exporter: Turns Graphify's graph.json (NetworkX node-link data) into a
   strictly compliant OKF v0.1 bundle.
2. An Extractor: Reads an OKF v0.1 bundle and reconstructs a Graphify-compatible
   knowledge graph with nodes and directed links.
"""

from __future__ import annotations
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
import yaml

# Helper to sanitize label to safe filename
def safe_filename(label: str) -> str:
    """Sanitize a string into a safe file name."""
    cleaned = re.sub(r'[\\/*?:"<>|#^[\]]', "", label.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")).strip()
    cleaned = re.sub(r"\.(md|mdx|qmd|markdown)$", "", cleaned, flags=re.IGNORECASE)
    # Ensure it starts with alphanumeric or fallback
    if not re.search(r"\w", cleaned, flags=re.UNICODE):
        return "unnamed_concept"
    # Replace spaces with underscores
    cleaned = cleaned.replace(" ", "_")
    # Limit to 100 bytes
    b = cleaned.encode("utf-8")
    if len(b) > 100:
        cleaned = b[:90].decode("utf-8", "ignore") + "_trunc"
    return cleaned


def export_to_okf_bundle(graph_json_path: str | Path, output_dir: str | Path) -> None:
    """Read a Graphify graph.json and export it as an OKF v0.1 bundle directory."""
    print(f"📥 Loading Graphify graph from: {graph_json_path}")
    with open(graph_json_path, "r", encoding="utf-8") as f:
        graph_data = json.load(f)

    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    concepts_root = root / "concepts"
    concepts_root.mkdir(parents=True, exist_ok=True)

    nodes = graph_data.get("nodes", [])
    links = graph_data.get("links", [])
    
    print(f"📊 Read {len(nodes)} nodes and {len(links)} links from graph.json.")

    # 1. Map nodes to helper dicts and compute community-to-nodes map
    node_by_id = {n["id"]: n for n in nodes}
    community_map: dict[str, list[dict]] = {}
    
    # Pre-generate unique filenames for each node to ensure consistent linking
    node_filenames: dict[str, str] = {}
    used_filenames: set[str] = set()
    
    for n in nodes:
        base = safe_filename(n.get("label", n["id"]))
        candidate = base
        idx = 1
        while candidate.lower() in used_filenames:
            candidate = f"{base}_{idx}"
            idx += 1
        used_filenames.add(candidate.lower())
        node_filenames[n["id"]] = candidate

    # Group by community
    for n in nodes:
        cid = n.get("community", 0)
        cname = n.get("community_name", f"Community_{cid}")
        c_dir_name = safe_filename(cname)
        n["_safe_filename"] = node_filenames[n["id"]]
        n["_community_dir"] = c_dir_name
        
        community_map.setdefault(cname, []).append(n)

    # Compile incoming/outgoing links per node
    outgoing_edges: dict[str, list[dict]] = {}
    incoming_edges: dict[str, list[dict]] = {}
    for link in links:
        src = link["source"]
        tgt = link["target"]
        rel = link.get("relation", "links")
        conf = link.get("confidence", "EXTRACTED")
        
        outgoing_edges.setdefault(src, []).append({"target": tgt, "relation": rel, "confidence": conf})
        incoming_edges.setdefault(tgt, []).append({"source": src, "relation": rel, "confidence": conf})

    # 2. Write Concept Files
    print("✍️ Generating concept files...")
    timestamp_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    
    for cname, cnodes in community_map.items():
        c_dir_name = safe_filename(cname)
        c_dir_path = concepts_root / c_dir_name
        c_dir_path.mkdir(parents=True, exist_ok=True)

        for n in cnodes:
            nid = n["id"]
            label = n.get("label", nid)
            ftype = n.get("file_type", "concept")
            src_file = n.get("source_file", "unknown")
            src_loc = n.get("source_location", "L1")

            # Map file type to compliant OKF type string
            okf_type = "Concept Node"
            if ftype == "code":
                okf_type = "Code Construct"
            elif ftype == "document":
                okf_type = "Technical Document"
            elif ftype == "paper":
                okf_type = "Academic Paper"

            # Prepare Frontmatter
            frontmatter = {
                "type": okf_type,
                "title": label,
                "description": f"Extracted code concept representing '{label}' at {src_loc} of {src_file}",
                "resource": src_file,
                "tags": [ftype, cname],
                "timestamp": timestamp_str,
                "graphify_id": nid
            }

            # Prepare Markdown Content
            md_lines = [
                "---",
                yaml.safe_dump(frontmatter, default_flow_style=False).strip(),
                "---",
                "",
                f"# {label}",
                "",
                f"This concept representing `{label}` was analyzed and extracted by Graphify.",
                "",
                "## 📍 Location",
                f"- **Source File:** `{src_file}`",
                f"- **Source Location:** `{src_loc}`",
                "",
                "## 🔗 Connections",
                ""
            ]

            # Outgoing links
            md_lines.append("### Outgoing Relations")
            out_list = outgoing_edges.get(nid, [])
            if not out_list:
                md_lines.append("- *No outgoing relations.*")
            else:
                for edge in out_list:
                    tgt_node = node_by_id.get(edge["target"])
                    if tgt_node:
                        tgt_fname = tgt_node["_safe_filename"]
                        tgt_cdir = tgt_node["_community_dir"]
                        tgt_label = tgt_node.get("label", edge["target"])
                        md_lines.append(
                            f"- --({edge['relation']})--> [{tgt_label}](/concepts/{tgt_cdir}/{tgt_fname}.md) "
                            f"(Confidence: *{edge['confidence']}*)"
                        )
            md_lines.append("")

            # Incoming links
            md_lines.append("### Incoming Relations")
            in_list = incoming_edges.get(nid, [])
            if not in_list:
                md_lines.append("- *No incoming relations.*")
            else:
                for edge in in_list:
                    src_node = node_by_id.get(edge["source"])
                    if src_node:
                        src_fname = src_node["_safe_filename"]
                        src_cdir = src_node["_community_dir"]
                        src_label = src_node.get("label", edge["source"])
                        md_lines.append(
                            f"- <--({edge['relation']})-- [{src_label}](/concepts/{src_cdir}/{src_fname}.md) "
                            f"(Confidence: *{edge['confidence']}*)"
                        )
            md_lines.append("")

            # Write file
            dest_file = c_dir_path / f"{n['_safe_filename']}.md"
            dest_file.write_text("\n".join(md_lines), encoding="utf-8")

    # 3. Write index.md (No Frontmatter)
    print("📝 Writing index.md...")
    index_lines = [
        "# Graphify Knowledge Catalog Index",
        "",
        "Welcome to the catalog generated from Graphify's Knowledge Graph. Under the hood, this directory adheres strictly to Google's Open Knowledge Format (OKF v0.1).",
        "",
        "## 📁 Concept Communities",
        ""
    ]
    for cname, cnodes in community_map.items():
        c_dir_name = safe_filename(cname)
        index_lines.append(f"### {cname}")
        for n in cnodes:
            index_lines.append(f"- [{n.get('label', n['id'])}](/concepts/{c_dir_name}/{n['_safe_filename']}.md)")
        index_lines.append("")

    (root / "index.md").write_text("\n".join(index_lines), encoding="utf-8")

    # 4. Write log.md
    print("📝 Writing log.md...")
    log_lines = [
        "# Change History Log",
        "",
        f"## {datetime.now(timezone.utc).strftime('%Y-%m-%d')}",
        "- Initialized and exported OKF Bundle from Graphify Knowledge Graph.",
        f"- Exported {len(nodes)} concepts divided across {len(community_map)} communities."
    ]
    (root / "log.md").write_text("\n".join(log_lines), encoding="utf-8")

    print(f"🎉 Success: OKF Bundle exported to: {root}")


def extract_okf_bundle_to_graph(bundle_dir: str | Path, output_graph_json: str | Path) -> None:
    """Ingest an OKF v0.1 bundle directory and build a Graphify-compliant graph.json."""
    root = Path(bundle_dir)
    print(f"📥 Loading OKF Bundle from: {bundle_dir}")

    nodes: list[dict] = []
    links: list[dict] = []

    concept_files = list(root.glob("concepts/**/*.md"))
    print(f"🔍 Found {len(concept_files)} concept markdown files in OKF bundle.")

    # Track safe filename to node ID mapping for edge resolution
    # Let's map e.g., "concepts/community_5/Value.md" -> its node ID
    path_to_node_id: dict[str, str] = {}

    # Step 1: Read all nodes and register them
    for file in concept_files:
        try:
            content = file.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) < 3:
                continue
            
            frontmatter = yaml.safe_load(parts[1])
            body = parts[2]

            label = frontmatter.get("title", file.stem)
            nid = file.stem.lower()  # unique identifier from filename
            
            # Map absolute-like relative path from bundle concepts root
            rel_bundle_path = f"/concepts/{file.relative_to(root / 'concepts').as_posix()}"
            path_to_node_id[rel_bundle_path] = nid
            # Also support bare filenames or absolute links
            path_to_node_id[f"/concepts/{file.relative_to(root / 'concepts')}".replace(".md", "")] = nid

            node_data = {
                "id": nid,
                "label": label,
                "file_type": "code" if frontmatter.get("type") == "Code Construct" else "document",
                "source_file": frontmatter.get("resource", "unknown"),
                "source_location": "L1",
                "community": 0,
                "community_name": frontmatter.get("tags", ["default"])[-1]
            }
            nodes.append(node_data)
        except Exception as exc:
            print(f"Error parsing concept {file}: {exc}")

    # Step 2: Parse body links to extract directed edges
    link_re = re.compile(r"\[([^\]]+)\]\((/concepts/[^\)]+)\)")
    for file in concept_files:
        try:
            content = file.read_text(encoding="utf-8")
            parts = content.split("---", 2)
            if len(parts) < 3:
                continue
            body = parts[2]
            
            src_path = f"/concepts/{file.relative_to(root / 'concepts').as_posix()}"
            src_id = path_to_node_id.get(src_path)
            if not src_id:
                continue

            matches = link_re.findall(body)
            for text, dest_path in matches:
                # Resolve destination node ID
                # Strip extension or query parts if any
                clean_dest = dest_path.split("?")[0].split("#")[0]
                dest_id = path_to_node_id.get(clean_dest) or path_to_node_id.get(clean_dest.replace(".md", ""))
                
                if dest_id and src_id != dest_id:
                    # To avoid duplicating edges, we only extract outgoing edges
                    # We can infer relation name from the text or surrounding markdown
                    # Let's see if the line contains a relation name like --(calls)-->
                    # Default relation is "references"
                    relation = "references"
                    if "calls" in text or "calls" in content:
                        relation = "calls"
                    elif "contains" in text:
                        relation = "contains"

                    link_data = {
                        "source": src_id,
                        "target": dest_id,
                        "relation": relation,
                        "confidence": "EXTRACTED",
                        "weight": 1.0
                    }
                    # Prevent duplicate links
                    if link_data not in links:
                        links.append(link_data)
        except Exception as exc:
            print(f"Error extracting links from {file}: {exc}")

    # Step 3: Write graph.json
    output_path = Path(output_graph_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    graph_out = {
        "directed": True,
        "multigraph": False,
        "graph": {},
        "nodes": nodes,
        "links": links
    }

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(graph_out, f, indent=2)

    print(f"🎉 Success: Ingested OKF Bundle into Graphify graph.json: {output_path}")


if __name__ == "__main__":
    # Smoke tests/demo when executed directly
    import sys
    if len(sys.argv) < 3:
        print("Usage:")
        print("  graphify_okf_integration_poc.py export <graph_json_path> <output_bundle_dir>")
        print("  graphify_okf_integration_poc.py extract <bundle_dir> <output_graph_json>")
        sys.exit(1)
        
    cmd = sys.argv[1]
    if cmd == "export":
        export_to_okf_bundle(sys.argv[2], sys.argv[3])
    elif cmd == "extract":
        extract_okf_bundle_to_graph(sys.argv[2], sys.argv[3])
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
