"""Document -> printer job, shared by the command line (cli.py) and the Printer Application command."""
import tempfile

from . import jobwriter, pipeline, render


def make_job(path, content_type="application/pdf", media="iso_a4_210x297mm", media_type="stationery", copies=1, collate=True,
             toner_save=False, options=None, job_name="document", user="user", host_name="lbp6030-printer-app",
             renderer="auto", fit=True):
    """returns (job bytes, number of pages in the document)"""
    paper_w, paper_h = jobwriter.PAPER[media][2:4]
    with tempfile.TemporaryDirectory() as td:
        pages, centre = render.load_document(path, content_type, paper_w, paper_h, td, renderer, fit=fit)
    if not pages:
        raise ValueError("the document has no pages")
    job = pipeline.build_gray(pages, media=media, media_type=media_type, copies=copies, collate=collate,
                                 options=options, toner_save=toner_save, centre=centre,
                                 job_name=job_name, user=user, host=host_name)
    return job, len(pages)
