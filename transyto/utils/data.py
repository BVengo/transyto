import os
import numpy as np
import tempfile

from transyto.utils import search_files_across_directories, fpack
from ccdproc import combine, subtract_bias, subtract_dark, flat_correct
from astropy.nddata import CCDData
from astropy.wcs import WCS
from astropy import units as u
from astropy.io import fits

from contextlib import suppress


def safe_load_ccdproc(fname, data_type):
    """Open fits file ensuring it has the right units for ccdproc.

    Make sure the units in BUNIT is what it should be. If BUNIT not in fits,
    then set to data_type input.

    Parameters
    ----------
    fname : string
        Fits file name to load.
    data_type : string
        Expected units of fits data.

    Returns
    -------

    data : ccdproc.CCDData
        Instance of ccdproc.CCDData with correct units specified.
    """
    try:
        data = CCDData.read(fname)
    except ValueError as err:
        if err.args[0] == "a unit for CCDData must be specified.":
            data = CCDData.read(fname, unit=data_type)
        else:
            raise(err)
    return data


def create_master_image_stack(filenames_path,
                              output_filename,
                              min_number_files_in_directory=3,
                              output_directory="./",
                              method="median",
                              scale=None,
                              **kwargs):
    """Create a master image stack.

    Search for files into list to create a master file.

    Args:
    ----------
    filenames_path : string
        Absolute path to location of files. OK if no data or not enough
        data is in the directory
    output_filename : string,
        Name of the output fits file. Include valid file extension.
    min_number_files_in_directory : int, optional
        Minimum number of required raw files to create master image
    output_directory : string
        Name of output directory, optional. Default is working directory.
    method : string, optional
        Method to combine fits images. Default method is median
    scale : array, optional
        scale to be used when combining images. Default is None.
    **kwargs
        Description

    Returns
    -------
    output_filename : string or None
        Master file, otherwise returns None.
    """

    # Check minimum amount of files to combine
    if len(filenames_path) < min_number_files_in_directory:
        print('EXIT: Not enough files in list for combining (returns None)')
        return None

    # # Print files in list to combine
    print('About to combine {} files'.format(len(filenames_path)))

    # Make the output directory for master file
    if output_directory == "./":
        path = os.path.join(filenames_path, "master")
        os.makedirs(path, exist_ok=True)
    else:
        os.makedirs(output_directory, exist_ok=True)

    # Get the ouput file name and path for master
    output_filename = os.path.join(output_directory, output_filename)

    # Remove existing file if it exists
    with suppress(FileNotFoundError):
        os.remove(output_filename)

    # Combine the file list to get the master data using any method
    combine(filenames_path, output_filename, method=method, scale=scale,
            combine_uncertainty_function=np.ma.std, unit="adu")

    # Print path of the master created
    print('CREATED (using {}): {}'.format(method, output_filename))

    return output_filename


def get_data(fname, *args, **kwargs):
    """Open fits file ensuring it has the right units for ccdproc.

    Make sure the units in BUNIT is what it should be. If BUNIT not in fits,
    then set to data_type input.

    Parameters
    ----------
    fname : string
        Fits file name to load.

    Returns
    -------

    data : data array
    """

    return fits.getdata(fname, *args, header=False, **kwargs)


def get_header(fn, *args, **kwargs):
    """Get the FITS header.

    Small wrapper around `astropy.io.fits.getheader` to auto-determine
    the FITS extension. This will return the header associated with the
    image. If you need the compression header information use the astropy
    module directly.

    Args:
        fn (str): Path to FITS file.
        *args: Passed to `astropy.io.fits.getheader`.
        **kwargs: Passed to `astropy.io.fits.getheader`.

    Returns:
        `astropy.io.fits.header.Header`: The FITS header for the data.
    """
    ext = 0
    if fn.endswith('.fz'):
        ext = 1
    return fits.getheader(fn, *args, ext=ext, **kwargs)


def get_value(fn, *args, **kwargs):
    """Get a value from the FITS header.

    Small wrapper around `astropy.io.fits.getval` to auto-determine
    the FITS extension. This will return the value from the header
    associated with the image (not the compression header). If you need
    the compression header information use the astropy module directly.

    Args:
        fn (str): Path to FITS file.

    Returns:
        str or float: Value from header (with no type conversion).
    """
    ext = 0
    if fn.endswith('.fz'):
        ext = 1
    return fits.getval(fn, *args, ext=ext, **kwargs)


def calibrate_data(filenames_path, darks_directory="", flats_directory="", bias_directory="",
                   flat_correction=True, verbose=True):
    """
    Does reduction of astronomical data by subtraction of dark noise
    and flat-fielding correction

    Parameters
    ----------
    filenames_path : string
        Top level path of .fits files to search for stars
    darks_directory : string
        Top level path of dark frames
    flats_directory : str, optional
        Top level path of flat frames
    bias_directory : str, optional
        Top level path of bias frames
    flat_correction : bool, optional
        Flag to perform flat/gain correction
    verbose : bool, optional
        Print each time an image is cleaned

    """

    # Temporary directory to create intermediate master files
    with tempfile.TemporaryDirectory() as tmp_directory:

        # Create and charge masterdark
        darks_list = search_files_across_directories(darks_directory, "*.fits*")
        masterdark = create_master_image_stack(darks_list, "masterdark.fits",
                                               output_directory=tmp_directory)
        masterdark = safe_load_ccdproc(masterdark, 'adu')

        if flat_correction:
            # Create and charge masterbias
            bias_list = search_files_across_directories(bias_directory, "*Bias*")
            masterbias = create_master_image_stack(bias_list, "masterdark.fits",
                                                   output_directory=tmp_directory)
            masterbias = safe_load_ccdproc(masterbias, 'adu')

            # Create and charge masterflat
            flats_list = search_files_across_directories(flats_directory, "*Flat*")
            masterflat = create_master_image_stack(flats_list, "masterflat.fits",
                                                   output_directory=tmp_directory)
            masterflat = safe_load_ccdproc(masterflat, 'adu')

            # Bias subtract the masterflat
            masterflat = subtract_bias(masterflat, masterbias)

            # Dark subtract the masterflat
            masterflat = subtract_dark(masterflat, masterdark,
                                       dark_exposure=(masterdark.
                                                      header["EXPTIME"] * u.s),
                                       data_exposure=(masterflat.
                                                      header["EXPTIME"] * u.s),
                                       scale=True)

        # List of science exposures to clean
        files_list = search_files_across_directories(filenames_path, "*fits*")

        # Output directory for files after reduction
        output_directory = filenames_path + "cleaned"
        os.makedirs(output_directory, exist_ok=True)

        # Reduce each science frame in files_list
        for fn in files_list:
            try:
                # Charge data of raw file and make dark subtraction
                raw_file = safe_load_ccdproc(fn, 'adu')

                reduced_file = subtract_dark(raw_file, masterdark,
                                             dark_exposure=(masterdark.
                                                            header["EXPTIME"] * u.s),
                                             data_exposure=(raw_file.
                                                            header["EXPTIME"] * u.s), scale=False)

                # Flat-field correction
                if flat_correction:
                    reduced_file = flat_correct(reduced_file, masterflat)

                # Save reduced science image to .fits file
                file_name = os.path.basename(fn)
                if fn.endswith(".fz"):
                    file_name = os.path.basename(fn).replace(".fz", "")
                science_image_cleaned_name = os.path.join(output_directory, "reduced_" + file_name)

                # Get header and WCS of raw files checking any extension
                header = get_header(fn)

                # Parse the WCS keywords and header in the primary HDU
                science_image_cleaned = CCDData(reduced_file, unit='adu',
                                                header=raw_file.header, wcs=WCS(header))
                # Write cleaned image
                science_image_cleaned.write(science_image_cleaned_name, overwrite=True)

                if os.path.isfile(science_image_cleaned_name) and verbose:
                    print("-> Cleaned: {}".format(science_image_cleaned_name))
                    fpack(science_image_cleaned_name)
            except ValueError:
                continue
