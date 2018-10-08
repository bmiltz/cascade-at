import os
import logging
from pathlib import Path
from pprint import pformat
import json

import pandas as pd
import numpy as np

from cascade.executor.argument_parser import DMArgumentParser
from cascade.input_data.db.demographics import age_groups_to_ranges
from cascade.dismod.db.wrapper import _get_engine
from cascade.dismod.db.metadata import DensityEnum
from cascade.testing_utilities import make_execution_context
from cascade.input_data.db.configuration import settings_for_model
from cascade.input_data.db.csmr import load_csmr_to_t3
from cascade.input_data.db.asdr import load_asdr_to_t3
from cascade.input_data.db.mortality import (
    get_cause_specific_mortality_data,
    get_age_standardized_death_rate_data,
)
from cascade.model.integrands import integrand_grids_from_gbd
from cascade.executor.no_covariate_main import bundle_to_observations, build_constraint
from cascade.executor.dismod_runner import run_and_watch, DismodATException
from cascade.input_data.configuration.form import Configuration
from cascade.input_data.db.bundle import bundle_with_study_covariates, freeze_bundle
from cascade.dismod.serialize import model_to_dismod_file
from cascade.model.integrands import make_average_integrand_cases_from_gbd
from cascade.saver.save_model_results import save_model_results
from cascade.input_data.configuration import SettingsError
from cascade.input_data.configuration.builder import (
    initial_context_from_epiviz,
    fixed_effects_from_epiviz,
    random_effects_from_epiviz,
)

CODELOG = logging.getLogger(__name__)
MATHLOG = logging.getLogger("cascade_a.math.runner")


def load_settings(meid=None, mvid=None, settings_file=None):
    if len([c for c in [meid, mvid, settings_file] if c is not None]) != 1:
        raise ValueError("Must supply exactly one of mvid, meid or settings_file")

    if settings_file is not None:
        with open(settings_file, "r") as f:
            raw_settings = json.load(f)
    else:
        raw_settings = settings_for_model(meid, mvid)

    settings = Configuration(raw_settings)
    errors = settings.validate_and_normalize()
    if errors:
        raise SettingsError("Configuration does not validate", errors, raw_settings)

    return settings


def execution_context_from_settings(settings):
    return make_execution_context(
        modelable_entity_id=settings.model.modelable_entity_id,
        model_version_id=settings.model.model_version_id,
        model_title=settings.model.title,
        gbd_round_id=settings.gbd_round_id,
        bundle_id=settings.model.bundle_id,
        add_csmr_cause=settings.model.add_csmr_cause,
        location_id=settings.model.drill_location,
    )


def meas_bounds_to_stdev(df):
    df["standard_error"] = (df.meas_upper - df.meas_lower) / (2 * 1.96)
    df["standard_error"] = df.standard_error.replace({0: 1e-9})
    df = df.rename(columns={"meas_value": "mean"})
    df["density"] = DensityEnum.gaussian
    df["weight"] = "constant"
    return df.drop(["meas_lower", "meas_upper"], axis=1)


def add_mortality_data(model_context, execution_context):
    csmr = meas_bounds_to_stdev(
        age_groups_to_ranges(execution_context, get_cause_specific_mortality_data(execution_context))
    )
    csmr["measure"] = "mtspecific"
    model_context.input_data.observations = pd.concat([model_context.input_data.observations, csmr])


def add_omega_constraint(model_context, execution_context):
    asdr = meas_bounds_to_stdev(
        age_groups_to_ranges(execution_context, get_age_standardized_death_rate_data(execution_context))
    )
    asdr["measure"] = "mtall"
    min_time = np.min(list(model_context.input_data.times))  # noqa: F841
    max_time = np.max(list(model_context.input_data.times))  # noqa: F841
    asdr = asdr.query("year_start >= @min_time and year_end <= @max_time and year_start % 5 == 0")
    model_context.rates.omega.parent_smooth = build_constraint(asdr)

    mask = model_context.input_data.observations.measure.isin(["mtall", "mtother", "mtspecific"])
    model_context.input_data.constraints = pd.concat([model_context.input_data.observations[mask], asdr])
    model_context.input_data.observations = model_context.input_data.observations[~mask]


def model_context_from_settings(execution_context, settings):
    model_context = initial_context_from_epiviz(settings)

    integrand_grids_from_gbd(model_context, execution_context)

    fixed_effects_from_epiviz(model_context, settings)
    random_effects_from_epiviz(model_context, settings)

    freeze_bundle(execution_context, execution_context.parameters.bundle_id)
    load_csmr_to_t3(execution_context)
    load_asdr_to_t3(execution_context)

    bundle, study_covariates = bundle_with_study_covariates(
        execution_context, bundle_id=model_context.parameters.bundle_id
    )
    bundle = bundle.query("location_id == @execution_context.parameters.location_id")
    model_context.input_data.observations = bundle_to_observations(model_context.parameters, bundle)
    mask = model_context.input_data.observations.standard_error > 0
    mask &= model_context.input_data.observations.measure != "relrisk"
    if mask.any():
        MATHLOG.warning("removing rows from bundle where standard_error == 0.0")
        model_context.input_data.observations = model_context.input_data.observations[mask]

    model_context.average_integrand_cases = make_average_integrand_cases_from_gbd(execution_context)
    add_mortality_data(model_context, execution_context)
    add_omega_constraint(model_context, execution_context)

    return model_context


def write_dismod_file(mc, db_file_path):
    dismod_file = model_to_dismod_file(mc)
    dismod_file.engine = _get_engine(Path(db_file_path))
    dismod_file.flush()
    return dismod_file


def run_dismod(dismod_file, with_random_effects):
    dm_file_path = dismod_file.engine.url.database
    if dm_file_path == ":memory:":
        raise ValueError("Cannot run dismodat on an in-memory database")

    command_prefix = ["dmdismod", dm_file_path]

    run_and_watch(command_prefix + ["init"], False, 1)
    dismod_file.refresh()
    if "end init" not in dismod_file.log.message.iloc[-1]:
        raise DismodATException("DismodAt failed to complete 'init' command")

    random_or_fixed = "both" if with_random_effects else "fixed"
    run_and_watch(command_prefix + ["fit", random_or_fixed], False, 1)
    dismod_file.refresh()
    if "end fit" not in dismod_file.log.message.iloc[-1]:
        raise DismodATException("DismodAt failed to complete 'fit' command")

    run_and_watch(command_prefix + ["predict", "fit_var"], False, 1)
    dismod_file.refresh()
    if "end predict" not in dismod_file.log.message.iloc[-1]:
        raise DismodATException("DismodAt failed to complete 'predict' command")


def has_random_effects(model):
    return any([bool(r.child_smoothings) for r in model.rates])


def main(args):
    settings = load_settings(args.meid, args.mvid, args.settings_file)

    ec = execution_context_from_settings(settings)
    mc = model_context_from_settings(ec, settings)

    ec.dismodfile = write_dismod_file(mc, args.db_file_path)

    run_dismod(ec.dismodfile, has_random_effects(mc))

    if not args.no_upload:
        save_model_results(ec)


def entry():
    parser = DMArgumentParser("Run DismodAT from Epiviz")
    parser.add_argument("db_file_path")
    parser.add_argument("--settings_file")
    parser.add_argument("--no-upload", action="store_true")
    parser.add_argument("--pdb", action="store_true")
    args, _ = parser.parse_known_args()

    try:
        main(args)
    except SettingsError as e:
        MATHLOG.error(str(e))
        MATHLOG.error(f"Form data: {pformat(e.form_data)}")
        MATHLOG.error(f"Form validation errors: {pformat(e.form_errors)}")
        exit(1)
    except Exception:
        if args.pdb:
            import pdb
            import traceback

            traceback.print_exc()
            pdb.post_mortem()
        else:
            CODELOG.exception(f"Uncaught exception in {os.path.basename(__file__)}")
            raise


if __name__ == "__main__":
    entry()