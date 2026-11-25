from __future__ import annotations

from pathlib import Path

from datasight.infrastructure.ingestion import renderer
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
      <div><head>Previous Work</head><p>Another paper used dataset GSE999.</p></div>
      <div><head>Literature Review</head><p>Earlier studies used dataset GSE888.</p></div>
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
    assert "Previous Work" in markdown
    assert "Literature Review" in markdown
    assert "Discussion" in markdown
    assert "Conclusion" in markdown
    assert "Supplementary Material" in markdown
    assert "Appendix" in markdown


def test_default_pruned_profile_keeps_scientific_sections(tmp_path: Path):
    xml = tmp_path / "paper.tei.xml"
    xml.write_text(_tei(), encoding="utf-8")
    markdown = extract_markdown(xml)
    assert "Methods" in markdown
    assert "Data Availability" in markdown
    assert "Previous Work" not in markdown
    assert "GSE999" not in markdown
    assert "Literature Review" not in markdown
    assert "GSE888" not in markdown
    assert "Discussion" in markdown
    assert "Conclusion" in markdown
    assert "Supplementary Material" in markdown
    assert "Appendix biobank" in markdown


def test_pruned_retains_dataset_back_matter_references_and_split_links(tmp_path: Path):
    xml = tmp_path / "dataset.tei.xml"
    xml.write_text("""<TEI xmlns="http://www.tei-c.org/ns/1.0"><text>
      <body><p>We evaluated both cohorts.</p></body>
      <back>
        <div type="availability"><div><head>Data availability</head><p>Both collections are available
        [https:// wiki. exam ple. org/ collec tion/ Alpha-Data, https:// exam ple. org/ archive/ Beta+ Cohort].
        The URL note [https://example.org/archive when available] is unchanged.</p></div></div>
        <div type="annex"><div><head>Datasets</head><p>We used the Alpha cohort for validation
        <ref type="bibr" target="#deposit">[2]</ref>. The Beta cohort was obtained from
        <ref target="https:// example.org/records/ 1234">the repository</ref>.</p></div></div>
        <div><head>Declarations</head><div><head>Competing interests</head><p>None declared.</p></div>
        <div><head>Materials availability</head><p>A separate subset is available.</p></div></div>
        <note place="foot" xml:id="n1">Subset accession GSE123456.</note>
        <listBibl>
          <biblStruct xml:id="deposit"><monogr><title>Data From Alpha</title>
          <idno type="DOI">10.5281/zenodo. 1234</idno>
          <ptr target="https://doi.org/10.5281/zenodo"/><ptr target="https://example.org/alpha"/>
          <ptr target="https://example.org/alpha/download"/></monogr></biblStruct>
          <bibl xml:id="uncited">Beta cohort archive, release 2.</bibl>
        </listBibl>
      </back>
    </text></TEI>""", encoding="utf-8")
    text = extract_markdown(xml)

    assert "Data availability" in text
    assert "[https://wiki.example.org/collection/Alpha-Data, https://example.org/archive/Beta+Cohort]" in text
    assert "[https://example.org/archive when available]" in text
    assert "https://example.org/records/1234" in text
    assert "Alpha cohort for validation [2]{#deposit}" in " ".join(text.split())
    assert "None declared" not in text
    assert "A separate subset is available." in text
    assert "Subset accession GSE123456." in text
    assert "{#deposit}" in text and "DOI: https://doi.org/10.5281/zenodo.1234" in text
    assert "URL: https://doi.org/10.5281/zenodo" not in text
    assert "URL: https://example.org/alpha URL: https://example.org/alpha/download" in text
    assert "{#uncited} Beta cohort archive, release 2." in text


def test_renderer_preserves_misparsed_appendix_rows_outside_the_bibliography(tmp_path: Path):
    xml = tmp_path / "paper.tei.xml"
    xml.write_text("""<TEI xmlns="http://www.tei-c.org/ns/1.0"><text>
      <body><p>We used Alpha and cite <ref type="bibr" target="#article">[1]</ref>.</p>
        <table><row><cell>Dataset</cell></row><row><cell>Alpha</cell></row></table></body>
      <back>
        <listBibl>
          <biblStruct xml:id="article"><monogr><title>Actual article</title></monogr></biblStruct>
          <biblStruct xml:id="caption"><analytic><title>Table A1. Overview of the 120 datasets used for model training</title></analytic></biblStruct>
          <biblStruct xml:id="row"><monogr><title>El</title><ptr target="https://zenodo.org/records/1234"/></monogr><note>Two merged rows</note></biblStruct>
        </listBibl>
        <listBibl><biblStruct xml:id="deposit"><monogr><title>Data From Alpha</title>
          <idno type="DOI">10.1234/alpha</idno></monogr></biblStruct></listBibl>
      </back></text></TEI>""", encoding="utf-8")
    text, metrics, warnings = renderer._render_document(xml, "pruned", renderer.RENDERER_VERSION)
    references, appendix = text.split(renderer.UNPARSED_TABLE_HEADING)
    assert "[1]{#article}" in references and "{#deposit}" in references
    assert "| Alpha |" in references and "{#row}" not in references
    assert "{#caption}" in appendix and "{#row}" in appendix
    assert "Two merged rows" in appendix and "https://zenodo.org/records/1234" in appendix
    assert metrics["unparsed_appendix_table_fragments"] == 2
    assert any("GROBID" in warning for warning in warnings)
    assert renderer.UNPARSED_TABLE_HEADING not in extract_markdown(xml, version=renderer.PREVIOUS_RENDERER_VERSION)


def test_renderer_versions_and_profiles_keep_distinct_default_artifacts(tmp_path: Path, monkeypatch):
    xml = tmp_path / "paper.tei.xml"
    xml.write_text(_tei(), encoding="utf-8")
    monkeypatch.setattr(renderer, "MARKDOWN_DIR", tmp_path / "renders")
    legacy = render_to_markdown(xml, version=renderer.LEGACY_RENDERER_VERSION)
    current = render_to_markdown(xml)
    full_body, = render_papers([{"paperId": current.paper_id, "xml_path": str(xml)}], profile="full_body")

    assert legacy.success and current.success and full_body.success
    assert len({legacy.md_path, current.md_path, full_body.md_path}) == 3
    assert legacy.md_path.read_bytes() == extract_markdown(xml, version=renderer.LEGACY_RENDERER_VERSION).encode()
    assert "Appendix biobank" not in legacy.md_path.read_text(encoding="utf-8")
    assert "Appendix biobank" in current.md_path.read_text(encoding="utf-8")
    assert current.producer_version == renderer.RENDERER_VERSION


def test_render_records_lineage_metrics_and_reuses_only_matching_cache(tmp_path: Path):
    xml = tmp_path / "paper.tei.xml"
    output = tmp_path / "paper.md"
    xml.write_text(_tei(), encoding="utf-8")

    first = render_to_markdown(xml, output, "W1")
    second = render_to_markdown(xml, output, "W1")

    assert first.success and first.sha256 and first.source_sha256
    assert first.profile == "pruned"
    assert first.warnings
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
    markdown = extract_markdown(xml, profile="full_body")
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
    text = extract_markdown(xml, profile="full_body")
    assert "[1]{#b1}" in text and "[[1]]" not in text
    assert "Appendix data" in text and "Back matter note" in text
    assert "{#b2}" in text and "Uncited Beta dataset, Version 2" in text


def test_references_collapse_xml_whitespace_and_keep_source_order(tmp_path):
    xml = tmp_path / "references.xml"
    xml.write_text('''<TEI xmlns="http://www.tei-c.org/ns/1.0"><text><body><p>Body.</p></body>
      <back><listBibl>
        <bibl xml:id="b1">First title
          <author>First author</author>
          2021</bibl>
        <bibl xml:id="b2">Second title</bibl>
        <bibl xml:id="b10">Tenth title</bibl>
      </listBibl></back></text></TEI>''', encoding="utf-8")

    text = extract_markdown(xml, profile="full_body")
    assert "{#b1} First title First author 2021\n\n" in text
    assert text.index("{#b1}") < text.index("{#b2}") < text.index("{#b10}")
