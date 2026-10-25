"""Document -> printer job, shared by the command line (cli.py) and the printer app (server.py)."""
import tempfile

from . import jobwriter, pipeline, render


def make_job(path, content_type="application/pdf", media="iso_a4_210x297mm", media_type="stationery", copies=1,
             collate=True, toner_save=False, options=None, job_name="document", user="user",
             host_name="lbp6030-printer-app", renderer="auto", fit=True, max_sheets=100):
    """returns (job bytes, number of pages in the document). Pages are rendered and encoded one at a time;
    documents needing more than max_sheets sheets are refused before the job is built."""
    paper_w, paper_h = jobwriter.PAPER[media][2:4]
    pages_done = [0]
    with tempfile.TemporaryDirectory() as td:
        count, pages = render.iter_document(path, content_type, paper_w, paper_h, td, renderer, fit=fit)
        if count is not None and count * copies > max_sheets:
            raise ValueError(f"{count} page(s) x {copies} copies is more than {max_sheets} sheets")

        def counted():
            for page in pages:
                pages_done[0] += 1
                if pages_done[0] * copies > max_sheets:
                    raise ValueError(f"more than {max_sheets} sheets")
                yield page
        job = pipeline.build_gray(counted(), media=media, media_type=media_type, copies=copies, collate=collate,
                                  options=options, toner_save=toner_save, job_name=job_name, user=user,
                                  host=host_name)
    if not pages_done[0]:
        raise ValueError("the document has no pages")
    return job, pages_done[0]
