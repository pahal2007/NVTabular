#
# Copyright (c) 2020, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
import collections
import logging
import time
import warnings

import dask
import dask_cudf
import yaml
from fsspec.core import get_fs_token_paths

from nvtabular.io.dask import _ddf_to_dataset
from nvtabular.io.dataset import Dataset, _set_dtypes
from nvtabular.io.shuffle import Shuffle, _check_shuffle_arg
from nvtabular.io.writer_factory import writer_factory
from nvtabular.ops import DFOperator, StatOperator, TransformOperator
from nvtabular.worker import clean_worker_cache

LOG = logging.getLogger("nvtabular")


class BaseWorkflow:

    """
    BaseWorkflow class organizes and runs all the feature engineering
    and pre-processing operators for your workflow.

    Parameters
    -----------
    cat_names : list of str
        Names of the categorical columns.
    cont_names : list of str
        Names of the continuous columns.
    label_name : list of str
        Names of the label column.
    config : object
    """

    def __init__(self, cat_names=None, cont_names=None, label_name=None, config=None):
        self.phases = []

        self.columns_ctx = {}
        self.columns_ctx["all"] = {}
        self.columns_ctx["continuous"] = {}
        self.columns_ctx["categorical"] = {}
        self.columns_ctx["label"] = {}
        self.columns_ctx["all"]["base"] = cont_names + cat_names + label_name
        self.columns_ctx["continuous"]["base"] = cont_names
        self.columns_ctx["categorical"]["base"] = cat_names
        self.columns_ctx["label"]["base"] = label_name

        self.stats = {}
        self.current_file_num = 0
        self.timings = {"write_df": 0.0, "preproc_apply": 0.0}
        if config:
            self.load_config(config)
        else:
            # create blank config and for later fill in
            self.config = get_new_config()

        self.clear_stats()

    def _get_target_cols(self, operators):
        # all operators in a list are chained therefore based on parent in list
        if type(operators) is list:
            target_cols = operators[0].get_default_in()
        else:
            target_cols = operators.get_default_in()
        return target_cols

    def _config_add_ops(self, operators, phase):
        """
        This function serves to translate the operator list api into backend
        ready dependency dictionary.

        Parameters
        ----------
        operators: list
            list of operators or single operator, Op/s to be added into the
            preprocessing phase
        phase:
            identifier for feature engineering FE or preprocessing PP
        """
        target_cols = self._get_target_cols(operators)
        # must have columns to target to
        if not target_cols or (
            target_cols in self.columns_ctx and not self.columns_ctx[target_cols]["base"]
        ):
            warnings.warn(f"Did not add operators: {operators}, target columns is empty.")
            return
        if phase in self.config and target_cols in self.config[phase]:
            self.config[phase][target_cols].append(operators)
            return

        warnings.warn(f"No main key {phase} or sub key {target_cols} found in config")

    def op_default_check(self, operators, default_in):
        if not type(operators) is list:
            operators = [operators]
        for op in operators:
            if op.default_in != default_in and op.default_in != "all":
                warnings.warn(
                    f"{op._id} was not added. This op is not designed for use"
                    f" with {default_in} columns"
                )
                operators.remove(op)
        return operators

    def add_feature(self, operators):
        """
        Adds feature engineering operator(s), while mapping
        to the correct columns given operator dependencies.

        Parameters
        -----------
        operators : object
            list of operators or single operator, Op/s to be
            added into the feature engineering phase
        """

        self._config_add_ops(operators, "FE")

    def add_cat_feature(self, operators):
        """
        Adds categorical feature engineering operator(s), while mapping
        to the correct columns given operator dependencies.

        Parameters
        -----------
        operators : object
            list of categorical operators or single operator, Op/s to be
            added into the feature engineering phase
        """

        operators = self.op_default_check(operators, "categorical")
        if operators:
            self.add_feature(operators)

    def add_cont_feature(self, operators):

        """
        Adds continuous feature engineering operator(s)
        to the workflow.

        Parameters
        -----------
        operators : object
            continuous objects such as FillMissing, Clip and LogOp
        """

        operators = self.op_default_check(operators, "continuous")
        if operators:
            self.add_feature(operators)

    def add_cat_preprocess(self, operators):

        """
        Adds categorical pre-processing operator(s)
        to the workflow.

        Parameters
        -----------
        operators : object
            categorical objects such as Categorify
        """

        operators = self.op_default_check(operators, "categorical")
        if operators:
            self.add_preprocess(operators)

    def add_cont_preprocess(self, operators):

        """
        Adds continuous pre-processing operator(s)
        to the workflow.

        Parameters
        -----------
        operators : object
            categorical objects such as Normalize
        """

        operators = self.op_default_check(operators, "continuous")
        if operators:
            self.add_preprocess(operators)

    def add_preprocess(self, operators):

        """
        Adds preprocessing operator(s), while mapping
        to the correct columns given operator dependencies.

        Parameters
        -----------
        operators : object
            list of operators or single operator, Op/s to be
            added into the preprocessing phase
        """
        # must add last operator from FE for get_default_in
        target_cols = self._get_target_cols(operators)
        if self.config["FE"][target_cols]:
            op_to_add = self.config["FE"][target_cols][-1]
        else:
            op_to_add = []
        if type(op_to_add) is list and op_to_add:
            op_to_add = op_to_add[-1]
        if op_to_add:
            op_to_add = [op_to_add]
        if type(operators) is list:
            op_to_add = op_to_add + operators
        else:
            op_to_add.append(operators)
        self._config_add_ops(op_to_add, "PP")

    def finalize(self):
        """
        When using operator list api, this allows the user to declare they
        have finished adding all operators and are ready to start processing
        data.
        """
        self.load_config(self.config)

    def load_config(self, config, pro=False):
        """
        This function extracts all the operators from the given phases and produces a
        set of phases with necessary operators to complete configured pipeline.

        Parameters
        ----------
        config : dict
            this object contains the phases and user specified operators
        pro: bool
            signals if config should be parsed via dependency dictionary or
            operator list api
        """
        # separate FE and PP
        if not pro:
            config = self._compile_dict_from_list(config)
        task_sets = {}
        master_task_list = []
        for task_set in config.keys():
            task_sets[task_set] = self._build_tasks(config[task_set], task_set, master_task_list)
            master_task_list = master_task_list + task_sets[task_set]

        baseline, leftovers = self._sort_task_types(master_task_list)
        if baseline:
            self.phases.append(baseline)
        self._phase_creator(leftovers)
        self._create_final_col_refs(task_sets)

    def _phase_creator(self, task_list):
        """
        task_list: list, phase specific list of operators and dependencies
        ---
        This function splits the operators in the task list and adds in any
        dependent operators i.e. statistical operators required by selected
        operators.
        """
        for task in task_list:
            added = False

            cols_needed = task[2].copy()
            if "base" in cols_needed:
                cols_needed.remove("base")
            for idx, phase in enumerate(self.phases):
                if added:
                    break
                for p_task in phase:
                    if not cols_needed:
                        break
                    if p_task[0]._id in cols_needed:
                        cols_needed.remove(p_task[0]._id)
                if not cols_needed and self._find_parents(task[3], idx):
                    added = True
                    phase.append(task)

            if not added:
                self.phases.append([task])

    def _find_parents(self, ops_list, phase_idx):
        """
        Attempt to find all ops in ops_list within subrange of phases
        """
        ops_copy = ops_list.copy()
        for op in ops_list:
            for phase in self.phases[:phase_idx]:
                if not ops_copy:
                    break
                for task in phase:
                    if not ops_copy:
                        break
                    if op._id in task[0]._id:
                        ops_copy.remove(op)
        if not ops_copy:
            return True

    def _sort_task_types(self, master_list):
        """
        This function helps ordering and breaking up the master list of operators into the
        correct phases.

        Parameters
        -----------
        master_list : list
            a complete list of all necessary operators to complete specified pipeline
        """
        nodeps = []
        for tup in master_list:
            if "base" in tup[2]:
                # base feature with no dependencies
                if not tup[3]:
                    master_list.remove(tup)
                    nodeps.append(tup)
        return nodeps, master_list

    def _compile_dict_from_list(self, config):
        """
        This function retrieves all the operators from the different keys in
        the config object.

        Parameters
        -----------
        config : dict
            this dictionary has phases(key) and the corresponding list of operators for
            each phase.
        """
        ret = {}
        for phase, task_list in config.items():
            ret[phase] = {}
            for k, v in task_list.items():
                tasks = []
                for obj in v:
                    if not isinstance(obj, collections.abc.Sequence):
                        obj = [obj]
                    for idx, op in enumerate(obj):
                        tasks.append((op, [obj[idx - 1]._id] if idx > 0 else []))

                ret[phase][k] = tasks
        return ret

    def _create_final_col_refs(self, task_sets):
        """
        This function creates a reference of all the operators whose produced
        columns will be available in the final set of columns. First step in
        creating the final columns list.
        """

        if "final" in self.columns_ctx.keys():
            return
        final = {}
        # all preprocessing tasks have a parent operator, it could be None
        # task (operator, main_columns_class, col_sub_key,  required_operators)
        for task in task_sets["PP"]:
            # an operator cannot exist twice
            if not task[1] in final.keys():
                final[task[1]] = []
            # detect incorrect dependency loop
            for x in final[task[1]]:
                if x in task[2]:
                    final[task[1]].remove(x)
            # stats dont create columns so id would not be in columns ctx
            if not task[0].__class__.__base__ == StatOperator:
                final[task[1]].append(task[0]._id)
        # add labels too specific because not specifically required in init
        final["label"] = []
        for col_ctx in self.columns_ctx["label"].values():
            if not final["label"]:
                final["label"] = ["base"]
            else:
                final["label"] = final["label"] + col_ctx
        # if no operators run in preprocessing we grab base columns
        if "continuous" not in final:
            # set base columns
            final["continuous"] = ["base"]
        if "categorical" not in final:
            final["categorical"] = ["base"]
        if "all" not in final:
            final["all"] = ["base"]
        self.columns_ctx["final"] = {}
        self.columns_ctx["final"]["ctx"] = final

    def create_final_cols(self):
        """
        This function creates an entry in the columns context dictionary,
        not the references to the operators. In this method we detail all
        operator references with actual column names, and create a list.
        The entry represents the final columns that should be in finalized
        dataframe.
        """
        # still adding double need to stop that
        final_ctx = {}
        for key, ctx_list in self.columns_ctx["final"]["ctx"].items():
            to_add = None
            for ctx in ctx_list:
                if ctx not in self.columns_ctx[key].keys():
                    ctx = "base"
                to_add = (
                    self.columns_ctx[key][ctx]
                    if not to_add
                    else to_add + self.columns_ctx[key][ctx]
                )
            if key not in final_ctx.keys():
                final_ctx[key] = to_add
            else:
                final_ctx[key] = final_ctx[key] + to_add
        self.columns_ctx["final"]["cols"] = final_ctx

    def get_final_cols_names(self, col_type):
        """
        Returns all the column names after preprocessing and feature
        engineering.
        Parameters
        -----------
        col_type : str
        """
        col_names = []
        for c_names in self.columns_ctx[col_type].values():
            for name in c_names:
                if name not in col_names:
                    col_names.append(name)
        return col_names

    def _build_tasks(self, task_dict: dict, task_set, master_task_list):
        """
        task_dict: the task dictionary retrieved from the config
        Based on input config information
        """
        # task format = (operator, main_columns_class, col_sub_key,  required_operators)
        dep_tasks = []
        for cols, task_list in task_dict.items():
            for target_op, dep_grp in task_list:
                if isinstance(target_op, DFOperator):
                    # check that the required stat is grabbed
                    # for all necessary parents
                    for opo in target_op.req_stats:
                        # only add if it doesnt already exist
                        if not self._is_repeat_op(opo, cols, master_task_list):
                            dep_grp = dep_grp if dep_grp else ["base"]
                            dep_tasks.append((opo, cols, dep_grp, []))
                # after req stats handle target_op
                dep_grp = dep_grp if dep_grp else ["base"]
                parents = [] if not hasattr(target_op, "req_stats") else target_op.req_stats
                if not self._is_repeat_op(target_op, cols, master_task_list):
                    dep_tasks.append((target_op, cols, dep_grp, parents))
        return dep_tasks

    def _is_repeat_op(self, op, cols, master_task_list):
        """
        Helper function to find if a given operator targeting a column set
        already exists in the master task list.

        Parameters
        ----------
        op: operator;
        cols: str
            one of the following; continuous, categorical, all
        """
        for task_d in master_task_list:
            if op._id in task_d[0]._id and cols == task_d[1]:
                return True
        return False

    def _run_trans_ops_for_phase(self, gdf, tasks):
        for task in tasks:
            op, cols_grp, target_cols, _ = task
            if isinstance(op, DFOperator):
                gdf = op.apply_op(gdf, self.columns_ctx, cols_grp, target_cols, self.stats)
            elif isinstance(op, TransformOperator):
                gdf = op.apply_op(gdf, self.columns_ctx, cols_grp, target_cols=target_cols)
        return gdf

    def apply_ops(
        self, gdf, start_phase=None, end_phase=None, writer=None, output_path=None, dtypes=None
    ):
        """
        gdf: cudf dataframe
        Controls the application of registered preprocessing phase op
        tasks, can only be used after apply has been performed
        """
        # put phases that you want to run represented in a slice
        # dont run stat_ops in apply
        # run the PP ops
        start = start_phase if start_phase else 0
        end = end_phase if end_phase else len(self.phases)
        for phase_index in range(start, end):
            start = time.time()
            gdf = self._run_trans_ops_for_phase(gdf, self.phases[phase_index])
            self.timings["preproc_apply"] += time.time() - start
            if phase_index == len(self.phases) - 1 and writer and output_path:

                if writer.need_cal_col_names:
                    cat_names = self.get_final_cols_names("categorical")
                    cont_names = self.get_final_cols_names("continuous")
                    label_names = self.get_final_cols_names("label")
                    writer.set_col_names(labels=label_names, cats=cat_names, conts=cont_names)
                    writer.need_cal_col_names = False

                start_write = time.time()
                # Special dtype conversion
                gdf = _set_dtypes(gdf, dtypes)
                writer.add_data(gdf)
                self.timings["write_df"] += time.time() - start_write

        return gdf

    def _update_statistics(self, stat_op):
        self.stats.update(stat_op.stats_collected())

    def save_stats(self, path):
        main_obj = {}
        stats_drop = {}
        for name, stat in self.stats.items():
            if name not in stats_drop.keys():
                stats_drop[name] = stat
        main_obj["stats"] = stats_drop
        main_obj["columns_ctx"] = self.columns_ctx
        with open(path, "w") as outfile:
            yaml.safe_dump(main_obj, outfile, default_flow_style=False)

    def load_stats(self, path):
        def _set_stats(self, stats_dict):
            for key, stat in stats_dict.items():
                self.stats[key] = stat

        with open(path, "r") as infile:
            main_obj = yaml.safe_load(infile)
            _set_stats(self, main_obj["stats"])
            self.columns_ctx = main_obj["columns_ctx"]

    def clear_stats(self):
        self.stats = {}


def get_new_config():
    """
    boiler config object, to be filled in with targeted operator tasks
    """
    config = {}
    config["FE"] = {}
    config["FE"]["all"] = []
    config["FE"]["continuous"] = []
    config["FE"]["categorical"] = []
    config["PP"] = {}
    config["PP"]["all"] = []
    config["PP"]["continuous"] = []
    config["PP"]["categorical"] = []
    return config


class Workflow(BaseWorkflow):
    """
    Dask-based NVTabular Workflow Class
    """

    def __init__(self, client=None, **kwargs):
        super().__init__(**kwargs)
        self.ddf = None
        self.client = client
        self._shuffle_parts = False
        self._base_phase = 0

    def set_ddf(self, ddf, shuffle=None):
        if isinstance(ddf, (dask_cudf.DataFrame, Dataset)):
            self.ddf = ddf
            if shuffle is not None:
                self._shuffle_parts = shuffle
        else:
            raise TypeError("ddf type not supported.")

    def get_ddf(self):
        if self.ddf is None:
            raise ValueError("No dask_cudf frame available.")
        elif isinstance(self.ddf, Dataset):
            # Right now we can't distinguish between input columns and generated columns
            # in the dataset, we don't limit the columm set right now in the to_ddf call
            # (https://github.com/NVIDIA/NVTabular/issues/409 )
            return self.ddf.to_ddf(shuffle=self._shuffle_parts)
        return self.ddf

    @staticmethod
    def _aggregated_op(gdf, ops):
        for op in ops:
            columns_ctx, cols_grp, target_cols, logic, stats_context = op
            gdf = logic(gdf, columns_ctx, cols_grp, target_cols, stats_context)
        return gdf

    def _aggregated_dask_transform(self, ddf, transforms):
        # Assuming order of transforms corresponds to dependency ordering
        meta = ddf._meta
        for transform in transforms:
            columns_ctx, cols_grp, target_cols, logic, stats_context = transform
            meta = logic(meta, columns_ctx, cols_grp, target_cols, stats_context)
        return ddf.map_partitions(self.__class__._aggregated_op, transforms, meta=meta)

    def exec_phase(self, phase_index, record_stats=True, update_ddf=True):
        """
        Gather necessary column statistics in single pass.
        Execute statistics for one phase only (given by phase index),
        but (laxily) perform all transforms for current and previous phases.
        """
        transforms = []

        # Need to perform all transforms up to, and including,
        # the current phase (not only the current phase).  We do this
        # so that we can avoid persisitng intermediate transforms
        # needed for statistics.
        phases = range(self._base_phase, phase_index + 1)
        for ind in phases:
            for task in self.phases[ind]:
                op, cols_grp, target_cols, _ = task
                if isinstance(op, TransformOperator):
                    stats_context = self.stats if isinstance(op, DFOperator) else None
                    logic = op.apply_op
                    transforms.append(
                        (self.columns_ctx, cols_grp, target_cols, logic, stats_context)
                    )
                elif not isinstance(op, StatOperator):
                    raise ValueError("Unknown Operator Type")

        # Perform transforms as single dask task (per ddf partition)
        _ddf = self.get_ddf()
        if transforms:
            _ddf = self._aggregated_dask_transform(_ddf, transforms)

        stats = []
        if record_stats:
            for task in self.phases[phase_index]:
                op, cols_grp, target_cols, _ = task
                if isinstance(op, StatOperator):
                    stats.append((op.stat_logic(_ddf, self.columns_ctx, cols_grp, target_cols), op))
                    # TODO: Don't want to update the internal ddf here if we can
                    # avoid it.  It may be better to just add the new column?
                    if op._ddf_out is not None:
                        self.set_ddf(op._ddf_out)
                        # We are updating the internal `ddf`, so we shouldn't
                        # redo transforms up to this phase in later phases.
                        self._base_phase = phase_index

        # Compute statistics if necessary
        if stats:
            if self.client:
                for r in self.client.compute(stats):
                    computed_stats, op = r.result()
                    op.finalize(computed_stats)
                    self._update_statistics(op)
                    op.clear()
            else:
                for r in dask.compute(stats, scheduler="synchronous")[0]:
                    computed_stats, op = r
                    op.finalize(computed_stats)
                    self._update_statistics(op)
                    op.clear()
            del stats

        # Update interal ddf.
        # Cancel futures and delete _ddf if allowed.
        if transforms and update_ddf:
            self.set_ddf(_ddf)
        else:
            if self.client:
                self.client.cancel(_ddf)
            del _ddf

    def reorder_tasks(self):
        # Reorder the phases so that dependency-free stat ops
        # are performed in the first two phases. This helps
        # avoid the need to persist transformed data between
        # phases (when unnecessary).
        cat_stat_tasks = []
        cont_stat_tasks = []
        new_phases = []
        for idx, phase in enumerate(self.phases):
            new_phase = []
            for task in phase:
                targ = task[1]  # E.g. "categorical"
                deps = task[2]  # E.g. ["base"]
                if isinstance(task[0], StatOperator):
                    if deps == ["base"]:
                        if targ == "categorical":
                            cat_stat_tasks.append(task)
                        else:
                            cont_stat_tasks.append(task)
                    else:
                        # This stat op depends on a transform
                        new_phase.append(task)
                elif isinstance(task[0], TransformOperator):
                    new_phase.append(task)
            if new_phase:
                new_phases.append(new_phase)

        # Construct new phases
        self.phases = []
        if cat_stat_tasks:
            self.phases.append(cat_stat_tasks)
        if cont_stat_tasks:
            self.phases.append(cont_stat_tasks)
        self.phases += new_phases

    def apply(
        self,
        dataset,
        apply_offline=True,
        record_stats=True,
        shuffle=None,
        output_path="./ds_export",
        output_format="parquet",
        out_files_per_proc=None,
        num_io_threads=0,
        dtypes=None,
    ):
        """
        Runs all the preprocessing and feature engineering operators.
        Also, shuffles the data if a `shuffle` option is specified.

        Parameters
        -----------
        dataset : object
        apply_offline : boolean
            Runs operators in offline mode or not
        record_stats : boolean
            Record the stats in file or not. Only available
            for apply_offline=True
        shuffle : nvt.io.Shuffle enum
            How to shuffle the output dataset. Shuffling is only
            performed if the data is written to disk. For all options,
            other than `None` (which means no shuffling), the partitions
            of the underlying dataset/ddf will be randomly ordered. If
            `PER_PARTITION` is specified, each worker/process will also
            shuffle the rows within each partition before splitting and
            appending the data to a number (`out_files_per_proc`) of output
            files. Output files are distinctly mapped to each worker process.
            If `PER_WORKER` is specified, each worker will follow the same
            procedure as `PER_PARTITION`, but will re-shuffle each file after
            all data is persisted.  This results in a full shuffle of the
            data processed by each worker.  To improve performace, this option
            currently uses host-memory `BytesIO` objects for the intermediate
            persist stage. The `FULL` option is not yet implemented.
        output_path : string
            Path to write processed/shuffled output data
        output_format : {"parquet", "hugectr", None}
            Output format to write processed/shuffled data. If None,
            no output dataset will be written (and shuffling skipped).
        out_files_per_proc : integer
            Number of files to create (per process) after
            shuffling the data
        num_io_threads : integer
            Number of IO threads to use for writing the output dataset.
            For `0` (default), no dedicated IO threads will be used.
        dtypes : dict
            Dictionary containing desired datatypes for output columns.
            Keys are column names, values are datatypes.
        """

        # Check shuffle argument
        shuffle = _check_shuffle_arg(shuffle)

        # If no tasks have been loaded then we need to load internal config
        if not self.phases:
            self.finalize()

        # Gather statstics (if apply_offline), and/or transform
        # and write out processed data
        if apply_offline:
            self.build_and_process_graph(
                dataset,
                output_path=output_path,
                record_stats=record_stats,
                shuffle=shuffle,
                output_format=output_format,
                out_files_per_proc=out_files_per_proc,
                num_io_threads=num_io_threads,
                dtypes=dtypes,
            )
        else:
            self.iterate_online(
                dataset,
                output_path=output_path,
                shuffle=shuffle,
                output_format=output_format,
                out_files_per_proc=out_files_per_proc,
                num_io_threads=num_io_threads,
                dtypes=dtypes,
            )

    def iterate_online(
        self,
        dataset,
        end_phase=None,
        output_path=None,
        shuffle=None,
        output_format=None,
        out_files_per_proc=None,
        apply_ops=True,
        num_io_threads=0,
        dtypes=None,
    ):
        """Iterate through dataset and (optionally) apply/shuffle/write."""
        # Check shuffle argument
        shuffle = _check_shuffle_arg(shuffle)

        # Check if we have a (supported) writer
        output_path = output_path or "./"
        output_path = str(output_path)
        writer = writer_factory(
            output_format,
            output_path,
            out_files_per_proc,
            shuffle,
            bytes_io=(shuffle == Shuffle.PER_WORKER),
            num_threads=num_io_threads,
        )

        # Iterate through dataset, apply ops, and write out processed data
        if apply_ops:
            columns = self.columns_ctx["all"]["base"]
            for gdf in dataset.to_iter(shuffle=(shuffle is not None), columns=columns):
                self.apply_ops(gdf, output_path=output_path, writer=writer, dtypes=dtypes)

        # Close writer and write general/specialized metadata
        if writer:
            general_md, special_md = writer.close()

            # Note that we "could" have the special and general metadata
            # written during `writer.close()` (just above) for the single-GPU case.
            # Instead, the metadata logic is separated from the `Writer` object to
            # simplify multi-GPU integration. When using Dask, we cannot assume
            # that the "shared" metadata files can/will be written by the same
            # process that writes the data.
            writer.write_special_metadata(special_md, writer.fs, output_path)
            writer.write_general_metadata(general_md, writer.fs, output_path)

    def update_stats(self, dataset, end_phase=None):
        """Colllect statistics only."""
        self.build_and_process_graph(dataset, end_phase=end_phase, record_stats=True)

    def build_and_process_graph(
        self,
        dataset,
        end_phase=None,
        output_path=None,
        record_stats=True,
        shuffle=None,
        output_format=None,
        out_files_per_proc=None,
        apply_ops=True,
        num_io_threads=0,
        dtypes=None,
    ):
        """Build Dask-task graph for workflow.

        Full graph is only executed if `output_format` is specified.
        """
        # Check shuffle argument
        shuffle = _check_shuffle_arg(shuffle)

        # Reorder tasks for two-phase workflows
        # TODO: Generalize this type of optimization
        self.reorder_tasks()

        end = end_phase if end_phase else len(self.phases)

        if output_format not in ("parquet", "hugectr", None):
            raise ValueError(f"Output format {output_format} not yet supported with Dask.")

        # Clear worker caches to be "safe"
        if self.client:
            self.client.run(clean_worker_cache)
        else:
            clean_worker_cache()

        self.set_ddf(dataset, shuffle=(shuffle is not None))
        if apply_ops:
            self._base_phase = 0  # Set _base_phase
            for idx, _ in enumerate(self.phases[:end]):
                self.exec_phase(idx, record_stats=record_stats, update_ddf=(idx == (end - 1)))
            self._base_phase = 0  # Re-Set _base_phase

        if dtypes:
            ddf = self.get_ddf()
            _meta = _set_dtypes(ddf._meta, dtypes)
            self.set_ddf(ddf.map_partitions(_set_dtypes, dtypes, meta=_meta))

        if output_format:
            output_path = output_path or "./"
            output_path = str(output_path)
            self.ddf_to_dataset(
                output_path,
                output_format=output_format,
                shuffle=shuffle,
                out_files_per_proc=out_files_per_proc,
                num_threads=num_io_threads,
            )

    def write_to_dataset(
        self,
        path,
        dataset,
        apply_ops=False,
        out_files_per_proc=None,
        shuffle=None,
        output_format="parquet",
        iterate=False,
        nfiles=None,
        num_io_threads=0,
        dtypes=None,
    ):
        """Write data to shuffled parquet dataset.

        Assumes statistics are already gathered.
        """
        # Check shuffle argument
        shuffle = _check_shuffle_arg(shuffle)

        if nfiles:
            warnings.warn("nfiles is deprecated. Use out_files_per_proc")
            if out_files_per_proc is None:
                out_files_per_proc = nfiles
        out_files_per_proc = out_files_per_proc or 1

        path = str(path)
        if iterate:
            self.iterate_online(
                dataset,
                output_path=path,
                shuffle=shuffle,
                output_format=output_format,
                out_files_per_proc=out_files_per_proc,
                apply_ops=apply_ops,
                num_io_threads=num_io_threads,
                dtypes=dtypes,
            )
        else:
            self.build_and_process_graph(
                dataset,
                output_path=path,
                record_stats=False,
                shuffle=shuffle,
                output_format=output_format,
                out_files_per_proc=out_files_per_proc,
                apply_ops=apply_ops,
                num_io_threads=num_io_threads,
                dtypes=dtypes,
            )

    def ddf_to_dataset(
        self,
        output_path,
        shuffle=None,
        out_files_per_proc=None,
        output_format="parquet",
        num_threads=0,
    ):
        """Dask-based dataset output.

        Currently supports parquet only.
        """
        if output_format not in ("parquet", "hugectr"):
            raise ValueError("Only parquet/hugectr output supported with Dask.")
        ddf = self.get_ddf()
        fs = get_fs_token_paths(output_path)[0]
        fs.mkdirs(output_path, exist_ok=True)
        if shuffle or out_files_per_proc:

            cat_names = self.get_final_cols_names("categorical")
            cont_names = self.get_final_cols_names("continuous")
            label_names = self.get_final_cols_names("label")

            # Output dask_cudf DataFrame to dataset
            _ddf_to_dataset(
                ddf,
                fs,
                output_path,
                shuffle,
                out_files_per_proc,
                cat_names,
                cont_names,
                label_names,
                output_format,
                self.client,
                num_threads,
            )
            return

        # Default (shuffle=None and out_files_per_proc=None)
        # Just use `dask_cudf.to_parquet`
        fut = ddf.to_parquet(output_path, compression=None, write_index=False, compute=False)
        if self.client is None:
            fut.compute(scheduler="synchronous")
        else:
            fut.compute()
