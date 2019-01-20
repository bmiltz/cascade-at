from collections import Iterable
from contextlib import contextmanager
from math import nan
from pathlib import Path
from subprocess import run, PIPE

import numpy as np
import pandas as pd

from cascade.core import getLoggers
from cascade.dismod.constants import DensityEnum, RateEnum, INTEGRAND_TO_WEIGHT, IntegrandEnum, COMMAND_IO
from cascade.dismod.db.wrapper import DismodFile, get_engine
from cascade.dismod.serialize import default_integrand_names, make_log_table
from cascade.model import Model
from cascade.model.model_reader import read_var_table_as_id, read_vars, write_vars
from cascade.model.model_writer import ModelWriter

CODELOG, MATHLOG = getLoggers(__name__)


class Session:
    def __init__(self, locations, parent_location, filename):
        """
        A session represents a connection with a Dismod-AT backend through
        a single Dismod-AT db file, the sqlite file it uses for input and
        output.

        Args:
            locations (pd.DataFrame): Initialize here because data refers to this.
            parent_location (int): The session uses parent location to subset
                                   data, but it isn't in the model.
            filename (str|Path): Location of the Dismod db to overwrite.
        """
        self.dismod_file = DismodFile()
        self._filename = Path(filename)
        if self._filename.exists():
            MATHLOG.info(f"{self._filename} exists so overwriting it.")
            self._filename.unlink()
        self.dismod_file.engine = get_engine(self._filename)
        self.parent_location = parent_location

        self._create_node_table(locations)
        self._basic_db_setup()
        # From covariate name to the x_<number> name that is used internally.
        # The session knows this piece of information but not the covariate
        # reference values. This is here because the columns of avgint and data
        # need to be renamed before writing, and they aren't part of the model.
        self._covariate_rename = dict()
        for create_name in ["data", "avgint"]:
            setattr(self.dismod_file, create_name, self.dismod_file.empty_table(create_name))

    def fit(self, model, data, initial_guess=None):
        """This is a fit without a predict. If the model
        has random effects, this optimizes over both fixed
        and random effects.

        Args:
            model (Model): A model, possibly without scale vars.
            data (pd.DataFrame): Data to fit.
            initial_guess (Var): Starting point to look for solutions. If not
                given, then the mean of the priors is taken as the initial
                guess.

        Returns:
            DismodGroups[Var]: A set of fit var.
        """
        if model.random_effect:
            MATHLOG.info(f"Running fit both.")
            return self._fit("both", model, data, initial_guess)
        else:
            MATHLOG.info(f"Running fit fixed.")
            return self._fit("fixed", model, data, initial_guess)

    def fit_fixed(self, model, data, initial_guess=None):
        """Fits a model without optimizing over any random effects.
        It does apply constant child value priors, but other random effects
        are constrained to zero. (This is equivalent to fitting with
        ``bound_random` equal to zero.) This is useful when one uses fitting
        with no random effects as a starting point for fitting with
        random effects.

        Args:
            model (Model): A model, possibly without scale vars.
            data (pd.DataFrame): Data to fit.
            initial_guess (Var): Starting point to look for solutions. If not
                given, then the mean of the priors is taken as the initial
                guess.

        Returns:
            DismodGroups[Var]: A set of fit var.
        """
        return self._fit("fixed", model, data, initial_guess)

    def fit_random(self, model, data, initial_guess=None):
        """
        Fits the data with the model.
        This optimizes the random effects with the fixed effects set to their
        starting values. The fixed effects are unchanged.

        Args:
            model (Model): A model, possibly without scale vars.
            data (pd.DataFrame): Data to fit.
            initial_guess (Var): Starting point to look for solutions. If not
                given, then the mean of the priors is taken as the initial
                guess.

        Returns:
            DismodGroups[Var]: A set of fit var.
        """
        return self._fit("random", model, data, initial_guess)

    def _fit(self, fit_level, model, data, initial_guess):
        # Throw out all tables
        extremal = ({data.age_lower.min(), data.age_upper.max()},
                    {data.time_lower.min(), data.time_upper.max()})
        self.write_model(model, extremal)
        self.write_data(data)
        self._run_dismod(["init"])
        if model.scale_set_by_user:
            self.set_var("scale", model.scale)
        else:
            # Assign to the private variable because setting the property
            # indicates that the user of the API wants to set their own scale
            # instead of using the one Dismod-AT calculates during init.
            model._scale = self.get_var("scale")

        if initial_guess is not None:
            MATHLOG.info(f"Setting initial value for search from user argument.")
            self.set_var("start", initial_guess)
        # else use the one generated by the call to init, coming from the mean.
        self._run_dismod(["fit", fit_level])
        return self.get_var("fit")

    def predict(self, var, avgint, parent_location, weights=None, covariates=None):
        """Given rates, calculated the requested average integrands.

        Args:
            var (DismodGroups): Var objects with rates.
            avgint (pd.DataFrame): Request data in these ages, times, and
                locations. Columns are ``integrand`` (str), ``location``
                (location_id), ``age_lower`` (float), ``age_upper`` (float),
                ``time_lower`` (float), ``time_upper`` (float). The integrand
                should be one of the names in IntegrandEnum.
            parent_location: The id of the parent location.
            weights (Dict[Var]): Weights are estimates of ``susceptible``,
                ``with_condition``, and ``total`` populations, used to bias
                integrands with age or time extent. Each one is a single
                Var object.
            covariates (List[Covariate]): A list of Covariates, so that we know
                the name and reference value for each.

        Returns:
            (pd.DataFrame, pd.DataFrame): The predicted avgints, and a dataframe
            of those not predicted because their covariates are greater than
            ``max_difference`` from the ``reference`` covariate value.
            Columns in the ``predicted`` are ``predict_id``, ``sample_index``,
            ``avg_integrand`` (this is the value), ``location``, ``integrand``,
            ``age_lower``, ``age_upper``, ``time_lower``, ``time_upper``.
        """
        self._check_vars(var)
        model = Model.from_var(var, parent_location, weights=weights, covariates=covariates)
        extremal = ({avgint.age_lower.min(), avgint.age_upper.max()},
                    {avgint.time_lower.min(), avgint.time_upper.max()})
        self.write_model(model, extremal)
        self.dismod_file.avgint = self.write_avgint(avgint)
        self._run_dismod(["init"])
        self.set_var("truth", var)
        self._run_dismod(["predict", "truth_var"])
        predicted, not_predicted = self.get_predict()
        return predicted, not_predicted

    def get_var(self, name):
        var_id = read_var_table_as_id(self.dismod_file)
        return read_vars(self.dismod_file, var_id, name)

    def set_var(self, name, new_vars):
        var_id = read_var_table_as_id(self.dismod_file)
        write_vars(self.dismod_file, new_vars, var_id, name)
        self.flush()

    def set_option(self, **kwargs):
        option = self.dismod_file.option
        unknowns = list()
        for name, value in kwargs.items():
            if not (option.option_name == name).any():
                unknowns.append(name)
            if isinstance(value, str):
                str_value = value
            elif isinstance(value, Iterable):
                str_value = " ".join(str(x) for x in value)
            else:
                str_value = str(value)
            option.loc[option.option_name == name, "option_value"] = str_value
        if unknowns:
            raise KeyError(f"Unknown options {unknowns}")

    @property
    def covariate_rename(self):
        return self._covariate_rename

    @covariate_rename.setter
    def covariate_rename(self, rename_dict):
        """Both the data and avgints need to have extra columns for covariates.
        Dismod-AT wants these defined, and at least an empty data and avgint
        table, before it will write the model. This step updates the list
        of covariates in the database schema before creating empty tables
        if necessary."""
        if set(rename_dict.values()) == set(self._covariate_rename.values()):
            self._covariate_rename = rename_dict
            return
        else:
            # Only rewrite schema if the x_<integer> list has changed.
            self._covariate_rename = rename_dict
        covariate_columns = list(sorted(self._covariate_rename.values()))
        for create_name in ["data", "avgint"]:
            empty = self.dismod_file.empty_table(create_name)
            without = [c for c in empty.columns if not c.startswith("x_")]
            # The wrapper needs these columns to have a dtype of Real.
            empty = empty[without].assign(**{cname: np.empty((0,), dtype=np.float) for cname in covariate_columns})
            self.dismod_file.update_table_columns(create_name, empty)
            if getattr(self.dismod_file, create_name).empty:
                CODELOG.debug(f"Writing empty {create_name} table with columns {covariate_columns}")
                setattr(self.dismod_file, create_name, empty)
            else:
                CODELOG.debug(f"Adding to {create_name} table schema the columns {covariate_columns}")

    def _run_dismod(self, command):
        """Pushes tables to the db file, runs Dismod-AT, and refreshes
        tables written."""
        self.flush()
        with self._close_db_while_running():
            completed_process = run(["dmdismod", str(self._filename)] + command, stdout=PIPE, stderr=PIPE)
            if completed_process.returncode != 0:
                MATHLOG.error(completed_process.stdout.decode())
                MATHLOG.error(completed_process.stderr.decode())
            assert completed_process.returncode == 0, f"return code is {completed_process.returncode}"
        if command[0] in COMMAND_IO:
            self.dismod_file.refresh(COMMAND_IO[command[0]].output)

    @contextmanager
    def _close_db_while_running(self):
        self.dismod_file.engine.dispose()
        try:
            yield
        finally:
            self.dismod_file.engine = get_engine(self._filename)

    @staticmethod
    def _check_vars(var):
        for group_name, group in var.items():
            for key, one_var in group.items():
                one_var.check(f"{group_name}-{key}")

    def write_model(self, model, extremal_age_time):
        writer = ModelWriter(self, extremal_age_time)
        model.write(writer)
        writer.close()
        self.flush()

    def flush(self):
        self.dismod_file.flush()

    def write_avgint(self, avgint):
        """
        Translate integrand name to id. Translate location to node.
        Add weight appropriate for this integrand. Writes to the Dismod file.

        Args:
            avgint (pd.DataFrame): Columns are ``integrand``, ``location``,
                ``age_lower``, ``age_upper``, ``time_lower``, ``time_upper``.
        """
        with_id = avgint.assign(integrand_id=avgint.integrand.apply(lambda x: IntegrandEnum[x].value))
        self._check_column_assigned(with_id, "integrand")
        with_weight = with_id.assign(weight_id=with_id.integrand.apply(lambda x: INTEGRAND_TO_WEIGHT[x].value))
        with_weight = with_weight.drop(columns=["integrand"]).reset_index(drop=True)
        with_location = with_weight.merge(
            self.dismod_file.node[["c_location_id", "node_id"]], left_on="location", right_on="c_location_id") \
            .drop(columns=["c_location_id", "location"])
        with_location = with_location.rename(columns=self.covariate_rename)
        return with_location.assign(avgint_id=with_location.index)

    @staticmethod
    def _check_column_assigned(with_id, column):
        column_id = f"{column}_id"
        if not with_id[with_id[column_id].isna()].empty:
            not_found_integrand = with_id[with_id[column_id].isna()][column].unique()
            kind_enum = globals()[f"{column.capitalize()}Enum"]
            err_message = (f"The {column} {not_found_integrand} weren't found in the "
                           f"{column} list {[i.name for i in kind_enum]}.")
            MATHLOG.error(err_message)
            raise RuntimeError(err_message)

    def read_avgint(self):
        avgint = self.dismod_file.avgint
        with_integrand = avgint.assign(integrand=avgint.integrand_id.apply(lambda x: IntegrandEnum(x).name))
        with_location = with_integrand.merge(self.dismod_file.node, on="node_id", how="left") \
            .rename(columns={"c_location_id": "location"})
        return with_location[
            ["avgint_id", "location", "integrand", "age_lower", "age_upper", "time_lower", "time_upper"]]

    def write_data(self, data):
        """
        Writes a data table. Locations can be any location and will be pruned
        to those that are descendants of the parent location.

        Args:
            data (pd.DataFrame): Columns are ``integrand``, ``location``,
                ``name``, ``hold_out``,
                ``age_lower``, ``age_upper``, ``time_lower``, ``time_upper``,
                ``density``, ``mean``, ``std``, ``eta``, ``nu``.
                The ``name`` is optional and will be assigned from the index.
                In addition, covariate columns are included. If ``hold_out``
                is missing, it will be assigned ``hold_out=0`` for not held out.
        """
        # Some of the columns get the same treatment as the average integrand.
        # This takes care of integrand and location.
        like_avgint = self.write_avgint(data).drop(columns=["avgint_id"])
        # Other columns have to do with priors.
        with_density = like_avgint.assign(density_id=like_avgint.density.apply(lambda x: DensityEnum[x].value))
        self._check_column_assigned(with_density, "density")
        with_density = with_density.reset_index(drop=True).drop(columns=["density"])
        if "name" not in with_density.columns:
            with_density = with_density.assign(name=with_density.index.astype(str))
        elif not with_density.name.isnull().empty:
            raise RuntimeError(f"There are some data values that lack data names.")
        else:
            pass  # There are data names everywhere.
        if "hold_out" not in with_density.columns:
            with_density = with_density.assign(hold_out=0)
        self.dismod_file.data = with_density.rename(columns={"mean": "meas_value", "std": "meas_std",
                                                             "name": "data_name"})

    def get_predict(self):
        avgint = self.read_avgint()
        raw = self.dismod_file.predict.merge(avgint, on="avgint_id", how="left")
        not_predicted = avgint[~avgint.avgint_id.isin(raw.avgint_id)]
        return raw.drop(columns=["avgint_id"]), not_predicted.drop(columns=["avgint_id"])

    def _basic_db_setup(self):
        """These things are true for all databases."""
        # Density table does not depend on model.
        self.dismod_file.density = pd.DataFrame({"density_name": [x.name for x in DensityEnum]})

        # Standard integrand naming scheme.
        all_integrands = default_integrand_names()
        self.dismod_file.integrand = all_integrands
        # Fill in the min_meas_cv later if required. Ensure integrand kinds have
        # known IDs early. Not nan because this "is non-negative and less than
        # or equal to one."
        self.dismod_file.integrand["minimum_meas_cv"] = 0

        self.dismod_file.rate = pd.DataFrame(dict(
            rate_id=[rate.value for rate in RateEnum],  # Will be 0-4.
            rate_name=[rate.name for rate in RateEnum],
            parent_smooth_id=nan,
            child_smooth_id=nan,
            child_nslist_id=nan,
        ))

        # Defaults, empty, b/c Brad makes them empty even if there are none.
        for create_name in ["nslist", "nslist_pair", "mulcov", "smooth_grid", "smooth"]:
            setattr(self.dismod_file, create_name, self.dismod_file.empty_table(create_name))
        self.dismod_file.log = make_log_table()
        self._create_options_table()

    def _create_options_table(self):
        # Options in grey were rejected by Dismod-AT despite being in docs.
        # https://bradbell.github.io/dismod_at/doc/option_table.htm
        option = pd.DataFrame([
            dict(option_name="parent_node_id", option_value=str(self.location_func(self.parent_location))),
            dict(option_name="parent_node_name", option_value=nan),
            dict(option_name="meas_std_effect", option_value="add_std_scale_all"),
            dict(option_name="zero_sum_random", option_value=nan),
            dict(option_name="data_extra_columns", option_value=nan),
            dict(option_name="avgint_extra_columns", option_value=nan),
            dict(option_name="warn_on_stderr", option_value="true"),
            dict(option_name="ode_step_size", option_value="5.0"),
            dict(option_name="age_avg_split", option_value=nan),
            dict(option_name="random_seed", option_value="0"),
            dict(option_name="rate_case", option_value="iota_pos_rho_zero"),
            dict(option_name="derivative_test_fixed", option_value="none"),
            dict(option_name="derivative_test_random", option_value="none"),
            dict(option_name="max_num_iter_fixed", option_value="100"),
            dict(option_name="max_num_iter_random", option_value="100"),
            dict(option_name="print_level_fixed", option_value=5),
            dict(option_name="print_level_random", option_value=5),
            dict(option_name="accept_after_max_steps_fixed", option_value="5"),
            dict(option_name="accept_after_max_steps_random", option_value="5"),
            dict(option_name="tolerance_fixed", option_value="1e-8"),
            dict(option_name="tolerance_random", option_value="1e-8"),
            dict(option_name="quasi_fixed", option_value="false"),
            dict(option_name="bound_frac_fixed", option_value="1e-2"),
            dict(option_name="limited_memory_max_history_fixed", option_value="30"),
            dict(option_name="bound_random", option_value=nan),
        ], columns=["option_name", "option_value"])
        self.dismod_file.option = option.assign(option_id=option.index)

    def _create_node_table(self, locations):
        columns = dict(
            node_name=locations.name,
            parent=locations.parent,
        )
        # This adds c_location_id, if it's there.
        for add_column in [c for c in locations.columns if c.startswith("c_")]:
            columns[add_column] = locations[add_column]
        table = pd.DataFrame(columns)
        table["node_id"] = table.index

        def location_to_node_func(location_id):
            if np.isnan(location_id):
                return np.nan
            return np.where(table.c_location_id == location_id)[0][0]

        table["parent"] = table.parent.apply(location_to_node_func)
        self.dismod_file.node = table
        self.location_func = location_to_node_func
