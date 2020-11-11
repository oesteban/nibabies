# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
fMRIPrep base processing workflows
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. autofunction:: init_nibabies_wf
.. autofunction:: init_single_subject_wf

"""

import sys
import os
from copy import deepcopy

from nipype.pipeline import engine as pe
from nipype.interfaces import utility as niu

from .. import config
from fmriprep.interfaces import SubjectSummary, AboutSummary, DerivativesDataSink
# from .bold import init_func_preproc_wf


def init_nibabies_wf():
    """
    Build *NiBabies*'s pipeline.

    This workflow organizes the execution of NiBabies, with a sub-workflow for
    each subject.

    If FreeSurfer's ``infant_recon_all`` is to be run, a corresponding folder is created
    and populated with any needed template subjects under the derivatives folder.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from nibabies.workflows.tests import mock_config
            from nibabies.workflows.base import init_nibabies_wf
            with mock_config():
                wf = init_nibabies_wf()

    """
    from niworkflows.engine.workflows import LiterateWorkflow as Workflow
    from niworkflows.interfaces.bids import BIDSFreeSurferDir

    nibabies_wf = Workflow(name="nibabies_wf")
    nibabies_wf.base_dir = config.execution.work_dir

    freesurfer = config.workflow.run_reconall
    if freesurfer:
        fsdir = pe.Node(
            BIDSFreeSurferDir(
                derivatives=config.execution.output_dir,
                freesurfer_home=os.getenv("FREESURFER_HOME"),
                spaces=config.workflow.spaces.get_fs_spaces(),
            ),
            name=f"fsdir_run_{config.execution.run_uuid.replace('-', '_')}",
            run_without_submitting=True,
        )
        if config.execution.fs_subjects_dir is not None:
            fsdir.inputs.subjects_dir = str(config.execution.fs_subjects_dir.absolute())

    for subject_id in config.execution.participant_label:
        single_subject_wf = init_single_subject_wf(subject_id)

        single_subject_wf.config["execution"]["crashdump_dir"] = str(
            config.execution.fmriprep_dir
            / f"sub-{subject_id}"
            / "log"
            / config.execution.run_uuid
        )
        for node in single_subject_wf._get_all_nodes():
            node.config = deepcopy(single_subject_wf.config)
        if freesurfer:
            nibabies_wf.connect(
                fsdir, "subjects_dir", single_subject_wf, "inputnode.subjects_dir"
            )
        else:
            nibabies_wf.add_nodes([single_subject_wf])

        # Dump a copy of the config file into the log directory
        log_dir = (
            config.execution.fmriprep_dir
            / f"sub-{subject_id}"
            / "log"
            / config.execution.run_uuid
        )
        log_dir.mkdir(exist_ok=True, parents=True)
        config.to_filename(log_dir / "nibabies.toml")

    return nibabies_wf


def init_single_subject_wf(subject_id):
    """
    Organize the preprocessing pipeline for a single subject.

    It collects and reports information about the subject, and prepares
    sub-workflows to perform anatomical and functional preprocessing.
    Anatomical preprocessing is performed in a single workflow, regardless of
    the number of sessions.
    Functional preprocessing is performed using a separate workflow for each
    individual BOLD series.

    Workflow Graph
        .. workflow::
            :graph2use: orig
            :simple_form: yes

            from nibabies.workflows.tests import mock_config
            from nibabies.workflows.base import init_single_subject_wf
            with mock_config():
                wf = init_single_subject_wf('01')

    Parameters
    ----------
    subject_id : :obj:`str`
        Subject label for this single-subject workflow.

    Inputs
    ------
    subjects_dir : :obj:`str`
        FreeSurfer's ``$SUBJECTS_DIR``.

    """
    from niworkflows.engine.workflows import LiterateWorkflow as Workflow
    from niworkflows.interfaces.bids import BIDSInfo, BIDSDataGrabber
    from niworkflows.interfaces.nilearn import NILEARN_VERSION
    from niworkflows.utils.bids import collect_data
    from niworkflows.utils.spaces import Reference

    from .anatomical import init_infant_anat_wf
    from ..utils.misc import fix_multi_source_name

    name = "single_subject_%s_wf" % subject_id
    subject_data = collect_data(
        config.execution.layout,
        subject_id,
        config.execution.task_id,
        config.execution.echo_idx,
        bids_filters=config.execution.bids_filters,
    )[0]

    if "flair" in config.workflow.ignore:
        subject_data["flair"] = []
    if "t2w" in config.workflow.ignore:
        subject_data["t2w"] = []

    anat_only = config.workflow.anat_only
    anat_derivatives = config.execution.anat_derivatives
    anat_modality = config.workflow.anat_modality
    spaces = config.workflow.spaces
    # Make sure we always go through these two checks
    if not anat_only and not subject_data["bold"]:
        task_id = config.execution.task_id
        raise RuntimeError(
            "No BOLD images found for participant {} and task {}. "
            "All workflows require BOLD images.".format(
                subject_id, task_id if task_id else "<all>"
            )
        )

    if anat_derivatives:
        from smriprep.utils.bids import collect_derivatives

        std_spaces = spaces.get_spaces(nonstandard=False, dim=(3,))
        anat_derivatives = collect_derivatives(
            anat_derivatives.absolute(),
            subject_id,
            std_spaces,
            config.workflow.run_reconall,
        )
        if anat_derivatives is None:
            config.loggers.workflow.warning(
                f"""\
Attempted to access pre-existing anatomical derivatives at \
<{config.execution.anat_derivatives}>, however not all expectations of fMRIPrep \
were met (for participant <{subject_id}>, spaces <{', '.join(std_spaces)}>, \
reconall <{config.workflow.run_reconall}>)."""
            )

    if not anat_derivatives and not subject_data[anat_modality]:
        raise Exception(
            f"No {anat_modality} images found for participant {subject_id}. "
            "All workflows require T1w images."
        )

    workflow = Workflow(name=name)
    workflow.__desc__ = """
Results included in this manuscript come from preprocessing
performed using *fMRIPrep* {fmriprep_ver}
(@fmriprep1; @fmriprep2; RRID:SCR_016216),
which is based on *Nipype* {nipype_ver}
(@nipype1; @nipype2; RRID:SCR_002502).

""".format(
        fmriprep_ver=config.environment.version,
        nipype_ver=config.environment.nipype_version,
    )
    workflow.__postdesc__ = """

Many internal operations of *fMRIPrep* use
*Nilearn* {nilearn_ver} [@nilearn, RRID:SCR_001362],
mostly within the functional processing workflow.
For more details of the pipeline, see [the section corresponding
to workflows in *fMRIPrep*'s documentation]\
(https://nibabies.readthedocs.io/en/latest/workflows.html \
"FMRIPrep's documentation").


### Copyright Waiver

The above boilerplate text was automatically generated by fMRIPrep
with the express intention that users should copy and paste this
text into their manuscripts *unchanged*.
It is released under the [CC0]\
(https://creativecommons.org/publicdomain/zero/1.0/) license.

### References

""".format(
        nilearn_ver=NILEARN_VERSION
    )

    fmriprep_dir = str(config.execution.fmriprep_dir)

    inputnode = pe.Node(
        niu.IdentityInterface(fields=["subjects_dir"]), name="inputnode"
    )

    bidssrc = pe.Node(
        BIDSDataGrabber(
            subject_data=subject_data,
            anat_only=anat_only,
            anat_derivatives=anat_derivatives,
            subject_id=subject_id,
        ),
        name="bidssrc",
    )

    bids_info = pe.Node(
        BIDSInfo(bids_dir=config.execution.bids_dir, bids_validate=False),
        name="bids_info",
    )

    summary = pe.Node(
        SubjectSummary(
            std_spaces=spaces.get_spaces(nonstandard=False),
            nstd_spaces=spaces.get_spaces(standard=False),
        ),
        name="summary",
        run_without_submitting=True,
    )

    about = pe.Node(
        AboutSummary(version=config.environment.version, command=" ".join(sys.argv)),
        name="about",
        run_without_submitting=True,
    )

    ds_report_summary = pe.Node(
        DerivativesDataSink(
            base_directory=fmriprep_dir,
            desc="summary",
            datatype="figures",
            dismiss_entities=("echo",),
        ),
        name="ds_report_summary",
        run_without_submitting=True,
    )

    ds_report_about = pe.Node(
        DerivativesDataSink(
            base_directory=fmriprep_dir,
            desc="about",
            datatype="figures",
            dismiss_entities=("echo",),
        ),
        name="ds_report_about",
        run_without_submitting=True,
    )

    # Preprocessing of anatomical (includes registration to UNCInfant)
    anat_preproc_wf = init_infant_anat_wf(
        age_months=config.workflow.age_months,
        anat_modality=anat_modality,
        anatomicals=subject_data[anat_modality],
        bids_root=config.execution.bids_dir,
        existing_derivatives=anat_derivatives,
        freesurfer=config.workflow.run_reconall,
        omp_nthreads=config.nipype.omp_nthreads,
        output_dir=fmriprep_dir,
        segmentation_atlases=config.execution.segmentation_atlases_dir,
        skull_strip_mode=config.workflow.skull_strip_t1w,
        skull_strip_template=Reference.from_string(
            config.workflow.skull_strip_template
        )[0],
        sloppy=config.execution.sloppy,
        spaces=spaces,
    )

    # fmt: off
    workflow.connect([
        (inputnode, anat_preproc_wf, [
            ('subjects_dir', 'inputnode.subjects_dir'),
        ]),
        (inputnode, summary, [
            ('subjects_dir', 'subjects_dir'),
        ]),
        (bidssrc, summary, [
            ('bold', 'bold'),
        ]),
        (bids_info, summary, [
            ('subject', 'subject_id'),
        ]),
        (bids_info, anat_preproc_wf, [
            (('subject', _prefix), 'inputnode.subject_id'),
        ]),
        (bidssrc, anat_preproc_wf, [
            ('t1w', 'inputnode.t1w'),
            ('t2w', 'inputnode.t2w'),
            # ('roi', 'inputnode.roi'),
            # ('flair', 'inputnode.flair'),
        ]),
        (summary, ds_report_summary, [
            ('out_report', 'in_file'),
        ]),
        (about, ds_report_about, [
            ('out_report', 'in_file'),
        ]),
    ])

    if not anat_derivatives:
        workflow.connect([
            (bidssrc, bids_info, [
                (('t1w', fix_multi_source_name), 'in_file'),
            ]),
            (bidssrc, summary, [
                ('t1w', 't1w'),
                ('t2w', 't2w'),
            ]),
            (bidssrc, ds_report_summary, [
                (('t1w', fix_multi_source_name), 'source_file'),
            ]),
            (bidssrc, ds_report_about, [
                (('t1w', fix_multi_source_name), 'source_file'),
            ]),
        ])
    else:
        workflow.connect([
            (bidssrc, bids_info, [
                (('bold', fix_multi_source_name), 'in_file'),
            ]),
            (anat_preproc_wf, summary, [
                ('outputnode.t1w_preproc', 't1w'),
            ]),
            (anat_preproc_wf, ds_report_summary, [
                ('outputnode.t1w_preproc', 'source_file'),
            ]),
            (anat_preproc_wf, ds_report_about, [
                ('outputnode.t1w_preproc', 'source_file'),
            ]),
        ])
    # fmt: on

    # Overwrite ``out_path_base`` of smriprep's DataSinks
    for node in workflow.list_node_names():
        if node.split(".")[-1].startswith("ds_"):
            workflow.get_node(node).interface.out_path_base = ""

    if anat_only:
        return workflow

    raise NotImplementedError("BOLD processing is not yet implemented.")

    # Append the functional section to the existing anatomical exerpt
    # That way we do not need to stream down the number of bold datasets
    anat_preproc_wf.__postdesc__ = (
        (anat_preproc_wf.__postdesc__ or "")
        + f"""

Functional data preprocessing

: For each of the {len(subject_data['bold'])} BOLD runs found per subject (across all
tasks and sessions), the following preprocessing was performed.
"""
    )

    for bold_file in subject_data["bold"]:
        func_preproc_wf = init_func_preproc_wf(bold_file)

        # fmt: off
        workflow.connect([
            (anat_preproc_wf, func_preproc_wf, [
                ('outputnode.anat_preproc', 'inputnode.anat_preproc'),
                ('outputnode.anat_mask', 'inputnode.anat_mask'),
                ('outputnode.anat_dseg', 'inputnode.anat_dseg'),
                ('outputnode.anat_aseg', 'inputnode.anat_aseg'),
                ('outputnode.anat_aparc', 'inputnode.anat_aparc'),
                ('outputnode.anat_tpms', 'inputnode.anat_tpms'),
                ('outputnode.template', 'inputnode.template'),
                ('outputnode.anat2std_xfm', 'inputnode.anat2std_xfm'),
                ('outputnode.std2anat_xfm', 'inputnode.std2anat_xfm'),
                # Undefined if --fs-no-reconall, but this is safe
                ('outputnode.subjects_dir', 'inputnode.subjects_dir'),
                ('outputnode.subject_id', 'inputnode.subject_id'),
                ('outputnode.anat2fsnative_xfm', 'inputnode.t1w2fsnative_xfm'),
                ('outputnode.fsnative2anat_xfm', 'inputnode.fsnative2t1w_xfm'),
            ]),
        ])
        # fmt: on

    return workflow


def _prefix(subid):
    return subid if subid.startswith("sub-") else f"sub-{subid}"