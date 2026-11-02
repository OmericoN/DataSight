from __future__ import annotations

from pathlib import Path

from datasight.infrastructure.ingestion.renderer import extract_markdown, render_papers, render_to_markdown


def _tei(table_rows: int = 12) -> str:
    rows = "".join(
        f"<row><cell>row-{index}</cell><cell>GSE{1000 + index}</cell></row>"
        for index in range(table_rows)
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader><fileDesc><titleStmt><title type="main">Evidence paper</title></titleStmt>
  <sourceDesc><biblStruct><analytic><author><persName><forename>Ada</forename><surname>Lovelace</surname></persName></author></analytic></biblStruct></sourceDesc>
  </fileDesc></teiHeader>
  <text>
    <body>
      <p>Opening paragraph uses the Alpha cohort.</p>
      <figure type="table"><head>Dataset identifiers</head><figDesc>DOI 10.5281/zenodo.1234</figDesc><table>{rows}</table></figure>
      <div><head>Methods</head><p>Data were retrieved from <ref target="https://zenodo.org/records/1234">Zenodo</ref>.</p>
        <list><item>First dataset item</item><item>Second corpus item</item></list>
        <figure><head>Workflow</head><figDesc>Accession PRJNA123456</figDesc><note>Figure note with Dryad.</note></figure>
      </div>
      <div><head>Data Availability</head><p>The data are available at https://doi.org/10.5281/zenodo.1234.</p></div>
      <div><head>Discussion</head><p>The cohort remains reusable.</p></div>
      <div><head>Conclusion</head><p>Dataset evidence is retained.</p></div>
      <div><head>Supplementary Material</head><p>Supplementary registry identifier ICPSR 12345.</p></div>
      <div><head>Appendix</head><p>Appendix biobank details.</p></div>
      <note>Top-level note mentions Figshare.</note>
    </body>
  </text>
</TEI>"""


def test_full_body_traverses_top_level_content_tables_lists_and_sections(tmp_path: Path):
    xml = tmp_path / "paper.tei.xml"
    xml.write_text(_tei(), encoding="utf-8")
    markdown = extract_markdown(xml, profile="full_body")

    assert "Dataset identifiers" in markdown
    assert "10.5281/zenodo.1234" in markdown
    assert "row-11" in markdown  # no ten-row truncation
    assert "PRJNA123456" in markdown
    assert "- First dataset item" in markdown
    assert "Top-level note mentions Figshare" in markdown
    assert "https://zenodo.org/records/1234" in markdown
    assert "Data Availability" in markdown
    assert "Discussion" in markdown
    assert "Conclusion" in markdown
    assert "Supplementary Material" in markdown
    assert "Appendix" in markdown


def test_explicit_pruned_profile_prunes_low_yield_sections(tmp_path: Path):
    xml = tmp_path / "paper.tei.xml"
    xml.write_text(_tei(), encoding="utf-8")
    markdown = extract_markdown(xml, profile="pruned")
    assert "Methods" in markdown
    assert "Data Availability" in markdown
    assert "Discussion" not in markdown
    assert "Conclusion" not in markdown
    assert "Supplementary Material" not in markdown
    assert "Appendix biobank" not in markdown


def test_render_records_lineage_metrics_and_reuses_only_matching_cache(tmp_path: Path):
    xml = tmp_path / "paper.tei.xml"
    output = tmp_path / "paper.md"
    xml.write_text(_tei(), encoding="utf-8")

    first = render_to_markdown(xml, output, "W1")
    second = render_to_markdown(xml, output, "W1")

    assert first.success and first.sha256 and first.source_sha256
    assert first.profile == "full_body"
    assert first.warnings == []
    assert first.quality_metrics
    assert first.quality_metrics["figures"] == 2
    assert first.quality_metrics["tables"] == 1
    assert first.quality_metrics["body_characters"] > 0
    assert second.success
    assert second.quality_metrics and second.quality_metrics["cache_reused"]

    output.write_text("tampered", encoding="utf-8")
    repaired = render_to_markdown(xml, output, "W1")
    assert repaired.success
    assert "Dataset identifiers" in output.read_text(encoding="utf-8")
    assert repaired.quality_metrics and not repaired.quality_metrics["cache_reused"]


def test_malformed_and_empty_tei_do_not_publish_markdown(tmp_path: Path):
    malformed = tmp_path / "malformed.xml"
    malformed.write_text("<TEI", encoding="utf-8")
    malformed_result = render_to_markdown(malformed, tmp_path / "malformed.md", "bad")
    assert not malformed_result.success
    assert not (tmp_path / "malformed.md").exists()

    empty = tmp_path / "empty.xml"
    empty.write_text(
        '<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body/></text></TEI>',
        encoding="utf-8",
    )
    empty_result = render_to_markdown(empty, tmp_path / "empty.md", "empty")
    assert not empty_result.success
    assert not (tmp_path / "empty.md").exists()


def test_citations_footnotes_abstract_formula_and_batch_failures_are_preserved(tmp_path: Path):
    xml = tmp_path / "references.tei.xml"
    xml.write_text(
        """<TEI xmlns="http://www.tei-c.org/ns/1.0">
        <teiHeader><fileDesc><titleStmt><title type="main">References</title></titleStmt><sourceDesc><p>source</p></sourceDesc></fileDesc>
        <profileDesc><abstract><p>Abstract dataset DOI 10.1/abstract.</p></abstract></profileDesc></teiHeader>
        <text><body><div><head n="1">Method</head><p>We used data <ref type="bibr" target="#b1">[7]</ref>
        and a note<ref type="foot" target="#n1">1</ref>. <ptr target="https://example.org/data"/>
        <formula>E = mc2</formula></p><table>plain table fallback</table></div>
        <note place="foot" xml:id="n1">Footnote dataset identifier PXD123456.</note></body>
        <back><listBibl><biblStruct xml:id="b1"><analytic><author><persName><surname>Smith</surname></persName></author>
        <title>Dataset paper</title></analytic><monogr><imprint><date when="2020"/></imprint></monogr>
        <idno type="DOI">10.1234/data</idno><ptr target="https://example.org/reference"/></biblStruct></listBibl></back>
        </text></TEI>""",
        encoding="utf-8",
    )
    markdown = extract_markdown(xml)
    assert "Abstract dataset DOI" in markdown
    assert "[7]" in markdown and "10.1234/data" in markdown
    assert "Footnote dataset identifier" in markdown
    assert "https://example.org/data" in markdown
    assert "E = mc2" in markdown
    assert "plain table fallback" in markdown

    results = render_papers(
        [
            {"paperId": "valid", "xml_path": str(xml)},
            {"paperId": "missing"},
        ],
        output_dir=tmp_path / "batch",
    )
    assert results[0].success
    assert not results[1].success


def test_full_body_preserves_back_matter_uncited_references_and_stable_citation_ids(tmp_path):
    xml = tmp_path / "full.xml"
    xml.write_text('''<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body>
      <p>We used Alpha <ref type="bibr" target="#b1">[1]</ref>.</p>
      </body><back><div><head>Appendix</head><p>Appendix data.</p></div>
      <note>Back matter note.</note><listBibl>
      <biblStruct xml:id="b1"><analytic><title>Alpha data</title></analytic></biblStruct>
      <bibl xml:id="b2">Uncited Beta dataset, Version 2.</bibl>
      </listBibl></back></text></TEI>''', encoding="utf-8")
    text = extract_markdown(xml)
    assert "[1]{#b1}" in text and "[[1]]" not in text
    assert "Appendix data" in text and "Back matter note" in text
    assert "{#b2}" in text and "Uncited Beta dataset, Version 2" in text
