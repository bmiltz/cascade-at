""" Functions for creating internal model representations of settings from EpiViz
"""
import logging
from collections import defaultdict

import numpy as np
import pandas as pd
from scipy.special import logit
from scipy import spatial

from cascade.model.covariates import Covariate, CovariateMultiplier
from cascade.model.grids import AgeTimeGrid, PriorGrid
from cascade.model.rates import Smooth
from cascade.input_data.configuration import SettingsError
from cascade.input_data.db.ccov import country_covariates
from cascade.core.context import ModelContext
from cascade.dismod.db.metadata import IntegrandEnum


MATHLOG = logging.getLogger(__name__)

RATE_TO_INTEGRAND = dict(
    iota=IntegrandEnum.Sincidence,
    rho=IntegrandEnum.remission,
    chi=IntegrandEnum.mtexcess,
    omega=IntegrandEnum.mtother,
    prevalence=IntegrandEnum.prevalence,
)


def identity(x): return x


def squared(x): return np.power(x, 2)


def scale1000(x): return x * 1000


COVARIATE_TRANSFORMS = {
    0: identity,
    1: np.log,
    2: logit,
    3: squared,
    4: np.sqrt,
    5: scale1000
}
"""
These functions transform covariate data, as specified in EpiViz.
"""


def initial_context_from_epiviz(configuration):
    context = ModelContext()
    context.parameters.modelable_entity_id = configuration.model.modelable_entity_id
    context.parameters.bundle_id = configuration.model.bundle_id
    context.parameters.gbd_round_id = configuration.gbd_round_id
    context.parameters.location_id = configuration.model.drill_location

    return context


def unique_country_covariate_transform(configuration):
    """
    Iterates through all covariate IDs, including the list of ways to
    transform them, because each transformation is its own column for Dismod.
    """
    seen_covariate = defaultdict(set)
    if configuration.country_covariate:
        for covariate_configuration in configuration.country_covariate:
            seen_covariate[covariate_configuration.country_covariate_id].add(covariate_configuration.transformation)

    for cov_id, cov_transformations in seen_covariate:
        yield cov_id, list(sorted(cov_transformations))


def assign_covariates(context):
    """
    The EpiViz interface allows assigning a covariate with a transformation
    to a specific target (rate, measure value, measure standard deviation).
    It is even the case, that the same covariate, say income, can be applied
    without transformation to iota on one smoothing and applied without
    transformation to chi with a *different smoothing*.
    Therefore, there can be multiple covariate columns built from the same
    covariate, one for each kind of transformation required.

    Args:
        context (ModelContext): model context that has age groups.
            The context is modified by this function. Covariate columns are
            added to input data and covariates are added to the list of
            covariates.

    Returns:
        function: This function is a map from the covariate identifier in the
            settings to the covariate name.
    """
    covariate_map = {}  # to find the covariates for covariate multipliers.

    # This walks through all unique combinations of covariates and their
    # transformations. Then, later, we apply them to particular target
    # rates, meas_values, meas_stds.
    for country_covariate_id, transforms in unique_country_covariate_transform(context.parameters):
        demographics = dict(
            age_group_ids="all",
            year_ids="all",
            sex_ids="all",
            location_ids=[context.parameters.location_id],
        )
        ccov_df = country_covariates(country_covariate_id, demographics)
        covariate_name = ccov_df.loc[0]["covariate_short_name"]

        # There is an order dependency from whether we interpolate before we
        # transform or transform before we interpolate.

        # Decide how to take the given data and extend / subset / interpolate.
        ccov_ranges_df = convert_age_year_ids_to_ranges(ccov_df, context.input_data.age_groups)

        for assign_to_measurement in [context.outputs.integrands]:
        desired_sex = [1, 2, 3]
        ccov_sexed_df = prune_covariate_sex(ccov_ranges_df, desired_sex)
        column_for_measurements = covariate_to_measurements_nearest_favoring_same_year(
            context.input_data.observations, ccov_sexed_df)

        for transform in transforms:
            # This happens per application to integrand.
            settings_transform = COVARIATE_TRANSFORMS[transform]
            transform_name = settings_transform.__name__
            MATHLOG.info(f"Transforming {covariate_name} with {transform_name}")
            name = f"{covariate_name}_{transform_name}"
            covariate_map[(covariate_name, transform)] = name
            ccov_df["value"] = settings_transform(column_for_measurements.mean_value)

            # The reference value is calculated from the download, not from the
            # the download as applied to the observations.
            reference = reference_value_for_covariate_mean_all_values(settings_transform(ccov_sexed_df))

            # Now attach the column to the observations.
            context.input_data.observations[f"x_{name}"] = ccov_df["value"]

            covariate_obj = Covariate(name, settings_transform(reference))
            context.input_data.covariates.append(covariate_obj)

    def column_id_func(covariate_search_name, transformation_id):
        return covariate_map[(covariate_search_name, transformation_id)]

    return column_id_func


def prune_covariate_sex(covariate_df, desired_sex):
    """
    The observations need to be pruned so that there is only one value
    per demographic interval, which means often using sex=male or female
    and dropping sex=both.

    Args:
        covariate_df (pd.DataFrame): Must have a sex_id column.
        desired_sex (List[int]): Nonempty list containing any of 1, 2, 3.

    Returns:
        pd.DataFrame: Same data as input with possibly-fewer rows and
            possible renaming of rows.
    """
    return covariate_df


def create_covariate_multipliers(context, column_id_func):
    # Assumes covariates exist.
    for mul_cov_config in context.parameters.country_covariate:
        smooth = make_smooth(mul_cov_config, context)
        covariate_obj = column_id_func(mul_cov_config.name, mul_cov_config.transformation)
        covariate_multiplier = CovariateMultiplier(covariate_obj, smooth)
        getattr(context.model, mul_cov_config.target).append(covariate_multiplier)


def reference_value_for_covariate_mean_all_values(cov_df):
    """Strategy for choosing reference value for country covariate."""
    return cov_df.mean()



def covariate_to_measurements_dummy(measurements, covariate):
    """
    Given a covariate that might not cover all of the age and time range
    of the measurements select a covariate value for each measurement.
    This version assigns 1.0 to every measurement.

    Args:
        measurements (pd.DataFrame):
            Columns include ``age_lower``, ``age_upper``, ``time_lower``,
            ``time_upper``. All others are ignored.
        covariate (pd.DataFrame):
            Columns include ``age_lower``, ``age_upper``, ``time_lower``,
            ``time_upper``, and ``value``.

    Returns:
        pd.Series: One row for every row in the measurements.
    """
    return pd.Series(np.ones((len(measurements),), dtype=np.float))


def covariate_to_measurements_nearest_favoring_same_year(measurements, covariates):
    """
    Given a covariate that might not cover all of the age and time range
    of the measurements select a covariate value for each measurement.
    This version chooses the covariate value whose mean age and time
    is closest to the mean age and time of the measurement in the same
    year. If that isn't found, it picks the covariate that is closest
    in age and time in the nearest year. In the case of a tie for distance,
    it averages.

    Args:
        measurements (pd.DataFrame):
            Columns include ``age_lower``, ``age_upper``, ``time_lower``,
            ``time_upper``. All others are ignored.
        covariate (pd.DataFrame):
            Columns include ``age_lower``, ``age_upper``, ``time_lower``,
            ``time_upper``, and ``value``.

    Returns:
        pd.Series: One row for every row in the measurements.
    """
    # Rescaling the age by 120 means that the nearest age within the year
    # will always be closer than the nearest time across a full year.
    tree = spatial.KDTree(list(zip(
        covariates[["age_lower", "age_upper"]].mean(axis=1) / 120,
        covariates[["time_lower", "time_upper"]].mean(axis=1)
    )))
    _, indices = tree.query(list(zip(
        measurements[["age_lower", "age_upper"]].mean(axis=1) / 120,
        measurements[["time_lower", "time_upper"]].mean(axis=1)
    )))
    return pd.Series(covariates.iloc[indices]["value"].values, index=measurements.index)


def convert_age_year_ids_to_ranges(with_ids_df, age_groups_df):
    """
    Converts ``age_group_id`` into ``age_lower`` and ``age_upper`` and
    ``year_id`` into ``time_lower`` and ``time_upper``. This treats the year
    as a range from start of year to start of the next year.

    Args:
        with_ids_df (pd.DataFrame): Has ``age_group_id`` and ``year_id``.
        age_groups_df (pd.DataFrame): Has columns ``age_group_id``,
            ``age_group_years_start``, and ``age_group_years_end``.

    Returns:
        pd.DataFrame: New pd.DataFrame with four added columns and in the same
            order as the input dataset.
    """
    original_order = with_ids_df.copy()
    # This "original index" guarantees that the order of the output dataset
    # and the index of the output dataset match that of with_ids_df, because
    # the merge reorders everything, including creation of a new index.
    original_order["original_index"] = original_order.index
    merged = pd.merge(original_order, age_groups_df, on="age_group_id", sort=False)
    if len(merged) != len(with_ids_df):
        # This is a fault in the input data.
        missing = set(with_ids_df.age_group_id.unique()) - set(age_groups_df.age_group_id.unique())
        raise RuntimeError(f"Not all age group ids from observations are found in the age group list {missing}")
    sorted = merged.sort_values(by="original_index").reset_index()
    sorted["time_lower"] = sorted["year_id"]
    sorted["time_upper"] = sorted["year_id"] + 1
    dropped = sorted.drop(columns=["age_group_id", "year_id", "original_index"])
    return dropped.rename(columns={"age_group_years_start": "age_lower", "age_group_years_end": "age_upper"})


def make_smooth(configuration, smooth_configuration):
    ages = smooth_configuration.age_grid
    if ages is None:
        ages = configuration.model.default_age_grid
    times = smooth_configuration.time_grid
    if times is None:
        times = configuration.model.default_time_grid
    grid = AgeTimeGrid(ages, times)

    d_time = PriorGrid(grid)
    d_age = PriorGrid(grid)
    value = PriorGrid(grid)

    d_age[:, :].prior = smooth_configuration.default.dage.prior_object
    d_time[:, :].prior = smooth_configuration.default.dtime.prior_object
    value[:, :].prior = smooth_configuration.default.value.prior_object

    if smooth_configuration.detail:
        for row in smooth_configuration.detail:
            if row.prior_type == "dage":
                pgrid = d_age
            elif row.prior_type == "dtime":
                pgrid = d_time
            elif row.prior_type == "value":
                pgrid = value
            else:
                raise SettingsError(f"Unknown prior type {row.prior_type}")
            pgrid[slice(row.age_lower, row.age_upper), slice(row.time_lower, row.time_upper)].prior = row.prior_object
    return Smooth(value, d_age, d_time)


def fixed_effects_from_epiviz(model_context, configuration):
    if configuration.rate:
        for rate_config in configuration.rate:
            rate_name = rate_config.rate
            if rate_name not in [r.name for r in model_context.rates]:
                raise SettingsError(f"Unspported rate {rate_name}")
            rate = getattr(model_context.rates, rate_name)
            rate.parent_smooth = make_smooth(configuration, rate_config)



def random_effects_from_epiviz(model_context, configuration):
    if configuration.random_effect:
        for smoothing_config in configuration.random_effect:
            rate_name = smoothing_config.rate
            if rate_name not in [r.name for r in model_context.rates]:
                raise SettingsError(f"Unspported rate {rate_name}")
            rate = getattr(model_context.rates, rate_name)
            location = smoothing_config.location
            rate.child_smoothings.append((location, make_smooth(configuration, smoothing_config)))
