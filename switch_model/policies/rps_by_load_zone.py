# Copyright 2017 The Switch Authors. All rights reserved.
# Licensed under the Apache License, Version 2, which is in the LICENSE file.

import os
from pyomo.environ import *
from switch_model.reporting import write_table

"""

This module defines a simple Renewable Portfolio Standard (RPS) policy scheme
for the Switch-Pyomo model. In this scheme, each fuel is categorized as RPS-
elegible or not. All non-fuel energy sources are assumed to be RPS-elegible.
Dispatched electricity that is generated by RPS-elegible sources in each
period is summed up and must meet an energy goal, set as a required percentage
of all energy that is generated in that period.

This module assumes that the generators.core.no_commit module is being used.
An error will be raised if this module is loaded along the
generators.core.commit package.

TODO:
Allow the usage of the commit module.

"""


def define_components(mod):
    """

    f_rps_eligible[f in FUELS] is a binary parameter that flags each fuel as
    elegible for RPS accounting or not.

    RPS_ENERGY_SOURCES is a set enumerating all energy sources that contribute
    to RPS accounting. It is built by union of all fuels that are RPS elegible
    and the NON_FUEL_ENERGY_SOURCES set.

    RPS_PERIODS is a subset of PERIODS for which RPS goals are defined.

    rps_target[z in LOAD_ZONES, p in RPS_PERIODS] is the fraction of total
    energy generated in a load zone during a period that has to be provided
    by RPS-elegible sources.

    RPSProjFuelPower[g, t in _FUEL_BASED_GEN_TPS] is an
    expression summarizing the power generated by RPS-elegible fuels in every
    fuel-based project. This cannot be simply taken to be equal to the
    dispatch level of the project, since a mix of RPS-elegible and unelegible
    fuels may be being consumed to produce that power. This expression is only
    valid when unit commitment is being ignored.

    RPSFuelEnergy[z, p] is an expression that sums all the energy produced in a
    load zone z using RPS-elegible fuels in fuel-based projects in a given period.

    RPSNonFuelEnergy[z, p] is an expression that sums all the energy produced
    in a load zone z using non-fuel sources in a given period.

    TotalGenerationInPeriod[p] is an expression that sums all the energy
    produced in a given period by all projects. This has to be calculated and
    cannot be taken to be equal to the total load in the period, because
    transmission losses could exist.

    RPS_Enforce_Target[z, p] is the constraint that forces energy produced by
    renewable sources in a load zone z to meet a fraction of the total energy
    produced in the period.

    Useful:
    GENS_IN_ZONE[z in LOAD_ZONES] is an indexed set that lists all
    generation projects within each load zone.

    """

    mod.ZONE_PERIODS = Set(dimen=2, within=mod.LOAD_ZONES * mod.PERIODS)

    mod.f_rps_eligible = Param(mod.FUELS, within=Boolean, default=False)
    mod.RPS_ENERGY_SOURCES = Set(
        initialize=lambda m: set(m.NON_FUEL_ENERGY_SOURCES)
        | set(f for f in m.FUELS if m.f_rps_eligible[f])
    )

    mod.RPS_PERIODS = Set(validate=lambda m, p: p in m.PERIODS)
    mod.rps_target = Param(
        mod.ZONE_PERIODS,
        within=NonNegativeReals,
        validate=lambda m, val, z, p: val <= 1.0,
    )

    mod.RPSFuelEnergy = Expression(
        mod.ZONE_PERIODS,
        rule=lambda m, z, p: sum(
            sum(
                m.GenFuelUseRate[g, t, f]
                for f in m.FUELS_FOR_GEN[g]
                if m.f_rps_eligible[f]
            )
            / m.gen_full_load_heat_rate[g]
            * m.tp_weight[t]
            for g in m.FUEL_BASED_GENS
            if g in m.GENS_IN_ZONE[z]
            for t in m.TPS_FOR_GEN_IN_PERIOD[g, p]
        ),
    )
    mod.RPSNonFuelEnergy = Expression(
        mod.ZONE_PERIODS,
        rule=lambda m, z, p: sum(
            m.DispatchGen[g, t] * m.tp_weight[t]
            for g in m.NON_FUEL_BASED_GENS
            if g in m.GENS_IN_ZONE[z]
            for t in m.TPS_FOR_GEN_IN_PERIOD[g, p]
        ),
    )

    mod.RPS_Enforce_Target = Constraint(
        mod.ZONE_PERIODS,
        rule=lambda m, z, p: (
            m.RPSFuelEnergy[z, p] + m.RPSNonFuelEnergy[z, p]
            >= m.rps_target[z, p] * m.zone_total_demand_in_period_mwh[z, p]
        ),
    )  # or mod.zone_total_demand_in_period_mwh


def total_generation_in_load_zone_in_period(model, z, period):
    return sum(
        model.DispatchGen[g, t] * model.tp_weight[t]
        for g in model.GENS_IN_ZONE[z]
        for t in model.TPS_FOR_GEN_IN_PERIOD[g, period]
    )


# [paty] not essential for this case. I'm leaving it there because it doesn't bother.
# def total_demand_in_period(model, period):
#    return sum(model.zone_total_demand_in_period_mwh[zone, period]
#               for zone in model.LOAD_ZONES)


def load_inputs(mod, switch_data, inputs_dir):
    """
    The RPS target goals input file is mandatory, to discourage people from
    loading the module if it is not going to be used. It is not necessary to
    specify targets for all periods.

    Mandatory input files:
        rps_targets.tab
            LOAD_ZONES PERIOD rps_target

    The optional parameter to define fuels as RPS eligible can be inputted
    in the following file:
        fuels.tab
            fuel  f_rps_eligible

    """

    switch_data.load_aug(
        filename=os.path.join(inputs_dir, "fuels.tab"),
        select=("fuel", "f_rps_eligible"),
        optional_params=["f_rps_eligible"],
        param=(mod.f_rps_eligible,),
    )
    switch_data.load_aug(
        filename=os.path.join(inputs_dir, "rps_targets.tab"),
        select=("load_zone", "period", "rps_target"),  # autoselect=True,
        index=mod.ZONE_PERIODS,  # mod.LOAD_ZONES * mod.PERIODS, #index=mod.RPS_PERIODS,
        # dimen=2,
        param=[mod.rps_target],
    )  # param=(mod.rps_target,))#param=(mod.LOAD_ZONES, mod.PERIODS, mod.rps_target,))


#    switch_data.load_aug(
#        filename=os.path.join(inputs_dir, 'fuel_cost.tab'),
#        select=('load_zone', 'fuel', 'period', 'fuel_cost'),
#        index=mod.ZONE_FUEL_PERIODS,
#        param=[mod.fuel_cost])


def post_solve(instance, outdir):
    """
    Export energy statistics relevant to RPS studies.

    """

    import switch_model.reporting as reporting

    def get_row(m, z, p):
        row = (
            z,
            p,
        )
        row += (m.RPSFuelEnergy[z, p] / 1000,)
        row += (m.RPSNonFuelEnergy[z, p] / 1000,)
        row += (total_generation_in_load_zone_in_period(m, z, p) / 1000,)
        row += (
            (m.RPSFuelEnergy[z, p] + m.RPSNonFuelEnergy[z, p])
            / total_generation_in_load_zone_in_period(m, z, p),
        )
        row += (m.zone_total_demand_in_period_mwh(m, z, p),)
        row += (
            (m.RPSFuelEnergy[z, p] + m.RPSNonFuelEnergy[z, p])
            / zone_total_demand_in_period_mwh(m, z, p),
        )
        return row

    reporting.write_table(
        instance,
        instance.LOAD_ZONES,
        instance.RPS_PERIODS,
        output_file=os.path.join(outdir, "rps_energy.txt"),
        headings=(
            "LOAD_ZONES",
            "PERIOD",
            "RPSFuelEnergyGWh",
            "RPSNonFuelEnergyGWh",
            "TotalGenerationInPeriodGWh",
            "RPSGenFraction",
            "TotalSalesInPeriodGWh",
            "RPSSalesFraction",
        ),
        values=get_row,
    )


def post_solve(instance, outdir):
    # write_table returns a tuple instead of expanding the indexes, so use
    # "gp" for the tuple instead of "g, p" for the components.
    write_table(
        instance,
        instance.ZONE_PERIODS,
        # instance, instance.LOAD_ZONES, instance.PERIODS,
        output_file=os.path.join(outdir, "rps_energy_v2.txt"),
        headings=(
            "LOAD_ZONE",
            "PERIOD",
            "RPSFuelEnergyGWh",
            "RPSNonFuelEnergyGWh",
            "TotalGenerationInPeriodGWh",
            "RPSGenFraction",
            "TotalSalesInPeriodGWh",
            "RPSSalesFraction",
        ),
        values=lambda m, (z, p): (
            z,
            p,
            m.RPSFuelEnergy[z, p] / 1000,
            m.RPSNonFuelEnergy[z, p] / 1000,
            total_generation_in_load_zone_in_period(m, z, p) / 1000,
            (m.RPSFuelEnergy[z, p] + m.RPSNonFuelEnergy[z, p])
            / total_generation_in_load_zone_in_period(m, z, p),
            m.zone_total_demand_in_period_mwh(z, p),
            (m.RPSFuelEnergy[z, p] + m.RPSNonFuelEnergy[z, p])
            / m.zone_total_demand_in_period_mwh(z, p),
        ),
    )
