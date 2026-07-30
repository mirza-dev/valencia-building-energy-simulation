class SetBuildingNorthAxis < OpenStudio::Measure::ModelMeasure
  def name
    'Set Building North Axis'
  end

  def description
    'Sets the OpenStudio Building north-axis angle while preserving every other model object.'
  end

  def modeler_description
    'A small built-in Phase 3 measure used to prove the browser measure runner and immutable provenance chain.'
  end

  def arguments(_model)
    args = OpenStudio::Measure::OSArgumentVector.new
    north_axis = OpenStudio::Measure::OSArgument.makeDoubleArgument('north_axis_deg', true)
    north_axis.setDisplayName('North axis')
    north_axis.setDescription('Clockwise rotation from true north in degrees. Values are normalized to 0–360°.')
    north_axis.setUnits('deg')
    north_axis.setDefaultValue(0.0)
    args << north_axis
    args
  end

  def run(model, runner, user_arguments)
    super(model, runner, user_arguments)
    return false unless runner.validateUserArguments(arguments(model), user_arguments)

    requested = runner.getDoubleArgumentValue('north_axis_deg', user_arguments)
    unless requested.finite?
      runner.registerError('north_axis_deg must be finite.')
      return false
    end

    normalized = requested % 360.0
    runner.registerWarning("Normalized #{requested}° to #{normalized}°.") if normalized != requested
    building = model.getBuilding
    before = building.northAxis
    building.setNorthAxis(normalized)
    runner.registerInitialCondition("Building north axis was #{before}°.")
    runner.registerFinalCondition("Building north axis is #{normalized}°.")
    true
  end
end

SetBuildingNorthAxis.new.registerWithApplication
