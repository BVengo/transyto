"""Defines TimeSeriesData"""

from __future__ import division
import os
import warnings
import logging

import pandas as pd
import numpy as np
import numpy.ma as ma
import time
import pyfiglet
import matplotlib.ticker as plticker

from astropy.coordinates import SkyCoord
from astropy.wcs import WCS
# from astropy.time import Time
from astropy.io import fits
from astropy.stats import sigma_clipped_stats
from astropy.stats import sigma_clip
# from astropy.io.fits import Undefined


from collections import namedtuple
from pathlib import Path
from operator import itemgetter
from wotan import flatten, t14
# from datetime import datetime
from matplotlib import pyplot as plt
from matplotlib import dates

from photutils.aperture.circle import CircularAperture, CircularAnnulus
from photutils import aperture_photometry
from photutils import centroid_2dg

from . import PACKAGEDIR
from .utils import (
    search_files_across_directories, bin_dataframe
)

__all__ = ['TimeSeriesData']

# Logger to track activity of the class
logger = logging.getLogger()

# warnings.filterwarnings('ignore', category=UserWarning, append=True)


class TimeSeriesData:
    """Photometry Class"""

    def __init__(self,
                 star_id,
                 data_directory,
                 search_pattern,
                 list_reference_stars,
                 aperture_radius,
                 centroid_box_width):
        """Initialize class Photometry for a given target and reference stars.

        Parameters
        ----------
        star_id : string
            Name of target star to do aperture photometry
        data_directory : string
            Top level path of .fits files to search for stars.
        search_pattern : string
            Pattern for searching files
        list_reference_stars : list
            Reference stars to be used in aperture photometry.
        aperture_radius : float
            Radius of the inner circular perture.
        centroid_box_width : float
            Width of the box to perform the centroid function.
        """

        # Positional Arguments
        self.star_id = star_id
        self.data_directory = data_directory
        self.search_pattern = search_pattern
        self.list_reference_stars = list_reference_stars

        # Aperture parameters
        self.r = aperture_radius
        self.r_in = aperture_radius * 1.6
        self.r_out = aperture_radius * 2.2

        # Centroid parameter
        self.box_w = centroid_box_width

        # Data bin
        self.binsize = 4

        # Output directory for logs
        logs_dir = self.data_directory + "logs_photometry"
        os.makedirs(logs_dir, exist_ok=True)

        logger.addHandler(logging.FileHandler(filename=os.path.join(logs_dir,
                                              'photometry.log'), mode='w'))

        logger.info(pyfiglet.figlet_format("-*-*-*-\n{}\n-*-*-*-".format(self.pipeline)))

        logger.info("{} will use {} reference stars for the photometry\n".
                    format(self.pipeline, len(self.list_reference_stars)))

    @property
    def pipeline(self):
        return os.path.basename(PACKAGEDIR)

    @property
    def readout(self):
        return self.get_keyword_value().readout

    @property
    def obs_time(self):
        return self.get_keyword_value().obstime

    @property
    def exptime(self):
        return self.get_keyword_value().exp

    @property
    def instrument(self):
        return self.get_keyword_value().instr

    @property
    def gain(self):
        return self.get_keyword_value().gain

    @property
    def keyword_list(self, telescope="TESS"):
        file = str(Path(__file__).parents[1]) + "/" + "telescope_keywords.csv"

        (Huntsman,
         TESS,
         WASP,
         MEARTH,
         POCS) = np.loadtxt(file, skiprows=2,
                            delimiter=";", dtype=str,
                            usecols=(1, 2, 3, 4, 5),
                            unpack=True)

        if telescope == "Huntsman":
            kw_list = Huntsman
        elif telescope == "TESS":
            kw_list = TESS

        return kw_list

    def get_keyword_value(self, default=None):
        """Returns a header keyword value.

        If the keyword is Undefined or does not exist,
        then return ``default`` instead.
        """

        try:
            kw_values = itemgetter(*self.keyword_list)(self.header)
        except KeyError:
            logger.error("Header keyword does not exist")
            return default
        exp, obstime, instr, readout, gain = kw_values

        Outputs = namedtuple("Outputs", "exp obstime instr readout gain")

        return Outputs(exp, obstime, instr, readout, gain)

    def make_aperture(self, data, coordinates, radius, r_in, r_out,
                      method="exact", subpixels=10):
        """Make the aperture sum in each positions for a given star. It
           can be rectangular (e.g. square), circular or annular

        Parameters
        ----------
        data : numpy array or CCDData
            contains the data where the aperture is going to be done
        coordinates : tuple
            (x, y) position of the star to do aperture
        radius : float
            Radius of the central aperture
        method : str, optional
            Method to be used for the aperture photometry
        r_in : int,
            Pixels added to central radius to get the inner radius
            of background aperture
        r_out : int,
            Pixels added to central radius to get the outer radius
            of background aperture
        subpixels : int, optional
            Number of subpixels for subpixel method. Each pixel
            is divided into subpixels**2.0

        Returns
        -------
        float
            Sum inside the aperture (sky background subtracted)

        """

        # Circular inner aperture for the star
        target_apertures = CircularAperture(coordinates, r=radius)

        # Annular outer aperture for the sky background
        background_apertures = CircularAnnulus(coordinates,
                                               r_in=r_in,
                                               r_out=r_out)

        # Find median value of counts-per-pixel in the background
        background_mask = background_apertures.to_mask(method="center")
        background_data = background_mask.multiply(data)
        mask = background_mask.data
        annulus_data_1d = background_data[mask > 0]
        (mean_sigclip,
         median_sigclip,
         std_sigclip) = sigma_clipped_stats(annulus_data_1d,
                                            sigma=3, maxiters=10)
        # sky_bkg = 3 * median_sigclip - 2 * mean_sigclip

        # Make aperture photometry for the object and the background
        apertures = [target_apertures, background_apertures]
        phot_table = aperture_photometry(data, apertures,
                                         method=method,
                                         subpixels=subpixels)

        # For consistent outputs in table
        for col in phot_table.colnames:
            phot_table[col].info.format = "%.8g"

        # Find median value of counts-per-pixel in the sky background.
        # sky_bkg = phot_table["aperture_sum_1"] / background_apertures.area
        sky_bkg = median_sigclip
        phot_table['background_median'] = sky_bkg

        # Find background in object inner aperture and subtract it
        background_in_target = sky_bkg * target_apertures.area

        phot_table["background_in_target"] = background_in_target
        phot_table["background_in_target"].info.format = "%.8g"

        assert phot_table["aperture_sum_0"] > phot_table["background_in_target"]

        object_final_counts = phot_table["aperture_sum_0"] - background_in_target

        # For consistent outputs in table
        phot_table["target_aperture_bkg_subtracted"] = object_final_counts
        phot_table["target_aperture_bkg_subtracted"].info.format = "%.8g"

        logger.info(phot_table["target_aperture_bkg_subtracted"])

        return (phot_table["target_aperture_bkg_subtracted"].item(),
                phot_table["background_in_target"].item())

    # @logged
    def get_star_data(self, star_id, data_directory, search_pattern):
        """Get all data from plate-solved images (right ascention,
           declination, airmass, dates, etc). Then, it converts the
           right ascention and declination into image positions to
           call make_aperture and find its total counts.

        Parameters
        ----------
        star_id: string
            name of star to be localized in each file
        data_directory: list
            list of files (frames) where we want to get the counts
        search_pattern: string
            pattern to search files

        Returns
        --------
        Counts of a star, list of good frames and airmass: tuple

        """
        star = SkyCoord.from_name(star_id)

        # Search for files containing data to analyze
        fits_files = search_files_across_directories(data_directory,
                                                     search_pattern)

        # List of ADU counts for the source, background
        object_counts = list()
        background_in_object = list()

        # List of exposure times
        exptimes = list()

        # List of object positions
        x_pos = list()
        y_pos = list()

        # Observation dates list
        times = list()

        # List of good frames
        self.good_frames_list = list()

        for fn in fits_files[0:600]:
            # Get data, header and WCS of fits files with any extension
            ext = 0
            if fn.endswith(".fz"):
                ext = 1
            data, self.header = fits.getdata(fn, header=True, ext=ext)
            wcs = WCS(self.header)

            # Check if WCS exist in image
            if wcs.is_celestial:

                # Star positions in the image
                y, x = wcs.all_world2pix(star.ra, star.dec, 0)

                if self.r_out > self.box_w:
                    logger.debug("Out of box. Choose a smaller outer radius.")
                    break

                sub_image = data[np.int(x) - self.box_w:np.int(x) + self.box_w,
                                 np.int(y) - self.box_w:np.int(y) + self.box_w]

                sigma = 1.0
                threshold = np.median(sub_image
                                      - (sigma * np.std(sub_image)))
                sub_image_cen = ma.masked_values(sub_image, threshold)

                with warnings.catch_warnings():
                    # Ignore warning for the centroid_2dg function
                    warnings.simplefilter('ignore', category=UserWarning)
                    x_cen, y_cen = centroid_2dg(sub_image_cen,
                                                mask=sub_image_cen.mask)

                # Exposure time
                exptimes.append(self.exptime * 24 * 60 * 60)

                # Observation times
                time = self.obs_time

                # Sum of counts inside aperture
                (counts_in_aperture,
                 bkg_in_object) = self.make_aperture(sub_image,
                                                     (x_cen, y_cen),
                                                     radius=self.r,
                                                     r_in=self.r_in,
                                                     r_out=self.r_out)

                object_counts.append(counts_in_aperture)
                background_in_object.append(bkg_in_object)
                x_pos.append(x)
                y_pos.append(y)
                times.append(time)
                self.good_frames_list.append(fn)
            else:
                continue

        return (object_counts, background_in_object,
                exptimes, x_pos, y_pos, times)

    # @logged
    def do_photometry(self,
                      save_rms=False,
                      detrend_data=False,
                      R_star=None,
                      M_star=None,
                      Porb=None):
        """Find the flux of a target star relative to some reference stars,
           using the counts inside an aperture

        Parameters
        ----------
        save_rms : bool, optional
            Save a txt file with the rms achieved for each time that
            the class is executed (defaul is False)
        detrend_data : bool, optional (default is False)
            If True, detrending of the time series data will be performed
        R_star : None, optional
            Radius of the star (in solar units). It has to be specified if
            detrend_data is True.
        M_star : None, optional
            Mass of the star (in solar units). It has to be specified
            if detrend_data is True.
        Porb : None, optional
            Orbital period of the planet (in days). It has to be specified if
            detrend_data is True.

        Returns
        -------
        relative flux : float
            The ratio between the target flux and the
            integrated flux of the reference stars
        """
        start = time.time()

        logger.info(f"Starting aperture photometry for {self.star_id}\n")

        # Get flux of target star
        (target_flux,
         background_in_object,
         exptimes,
         x_pos_target,
         y_pos_target,
         self.times) = self.get_star_data(self.star_id,
                                          self.data_directory,
                                          self.search_pattern)

        self.times = np.asarray(self.times)

        logger.info("Finished aperture photometry on target star. "
                    f"{self.__class__.__name__} will compute now the "
                    "combined flux of the ensemble\n")

        # Positions of target star
        self.x_pos_target = np.array(x_pos_target) - np.nanmean(x_pos_target)
        self.y_pos_target = np.array(y_pos_target) - np.nanmean(y_pos_target)

        # Target and background counts per second
        exptimes = np.asarray(exptimes)
        target_flux = np.asarray(target_flux)
        self.target_flux_sec = target_flux / exptimes
        background_in_target_sec = np.asarray(background_in_object) / exptimes

        # CCD gain
        ccd_gain = self.gain

        readout_noise = (self.readout * self.r)**2 * np.pi * np.ones(len(self.good_frames_list))

        # Sigma readout noise
        ron = np.sqrt(readout_noise)
        self.sigma_ron = -2.5 * np.log10((self.target_flux_sec * ccd_gain * exptimes - ron)
                                         / (self.target_flux_sec * ccd_gain * exptimes))

        # Sigma photon noise
        # self.sigma_phot = 1 / np.sqrt(self.target_flux_sec * ccd_gain * self.exptimes)
        self.sigma_phot = -2.5 * np.log10((self.target_flux_sec * ccd_gain * exptimes
                                           - np.sqrt(self.target_flux_sec * ccd_gain
                                                     * exptimes))
                                          / (self.target_flux_sec * ccd_gain * exptimes))

        # Sigma sky-background noise
        self.sigma_sky = -2.5 * np.log10((self.target_flux_sec * ccd_gain * exptimes
                                          - np.sqrt(background_in_target_sec * ccd_gain
                                                    * exptimes))
                                         / (self.target_flux_sec * ccd_gain * exptimes))

        # Total photometric error for 1 mag in one observation
        self.sigma_total = np.sqrt(self.sigma_phot**2.0 + self.sigma_ron**2.0
                                   + self.sigma_sky**2.0)

        # Signal to noise: shot, sky noise (per second) and readout
        S_to_N_obj_sec = self.target_flux_sec / np.sqrt(self.target_flux_sec
                                                        + background_in_target_sec
                                                        + readout_noise
                                                        / (ccd_gain * exptimes))
        # Convert SN_sec to actual SN
        S_to_N_obj = S_to_N_obj_sec * np.sqrt(ccd_gain * exptimes)

        # Get the flux of each reference star
        self.reference_star_flux_sec = list()
        background_in_ref_star_sec = list()
        for ref_star in self.list_reference_stars:

            logger.info(f"Starting aperture photometry on ref_star {ref_star}\n")

            (refer_flux,
             background_in_ref_star,
             exptimes_ref,
             x_pos_ref,
             y_pos_ref,
             obs_dates) = self.get_star_data(ref_star,
                                             self.data_directory,
                                             self.search_pattern)
            self.reference_star_flux_sec.append(np.asarray(refer_flux) / exptimes)
            background_in_ref_star_sec.append(np.asarray(background_in_ref_star) / exptimes)
            logger.info(f"Finished aperture photometry on ref_star {ref_star}\n")

        self.reference_star_flux_sec = np.asarray(self.reference_star_flux_sec)
        background_in_ref_star_sec = np.asarray(background_in_ref_star_sec)

        sigma_squared_ref = (self.reference_star_flux_sec * exptimes
                             + background_in_ref_star_sec * exptimes
                             + readout_noise)

        weights_ref_stars = 1.0 / sigma_squared_ref

        ref_flux_averaged = np.average(self.reference_star_flux_sec * exptimes,
                                       weights=weights_ref_stars,
                                       axis=0)

        # Integrated flux per sec for ensemble of reference stars
        total_reference_flux_sec = np.sum(self.reference_star_flux_sec, axis=0)

        # Integrated sky background for ensemble of reference stars
        total_reference_bkg_sec = np.sum(background_in_ref_star_sec, axis=0)

        # S/N for reference star per second
        S_to_N_ref_sec = total_reference_flux_sec / np.sqrt(total_reference_flux_sec
                                                            + total_reference_bkg_sec
                                                            + readout_noise
                                                            / (ccd_gain * exptimes))
        # Convert S/N per sec for ensemble to total S/N
        S_to_N_ref = S_to_N_ref_sec * np.sqrt(ccd_gain * exptimes)

        # Relative flux per sec of target star
        differential_flux = target_flux / ref_flux_averaged

        # Normalized relative flux
        self.normalized_flux = differential_flux / np.nanmedian(differential_flux)

        # Find Differential S/N for object and ensemble
        S_to_N_diff = 1 / np.sqrt(S_to_N_obj**-2 + S_to_N_ref**-2)

        # Ending time of computatin analysis.
        end = time.time()
        exec_time = end - start

        # Print when all of the analysis ends
        logger.info(f"Differential photometry of {self.star_id} has been finished, "
                    f"with {len(self.good_frames_list)} frames "
                    f"of camera {self.instrument} (run time: {exec_time:.3f} sec)\n")

        # Neglect outliers in the timeseries: create mask
        self.clipped_values_mask = sigma_clip(self.normalized_flux, sigma=10,
                                              maxiters=10, cenfunc=np.median,
                                              masked=True, copy=True)
        self.normalized_flux = self.normalized_flux[~self.clipped_values_mask.mask]
        self.times_clipped = self.times[~self.clipped_values_mask.mask]

        if detrend_data:
            logger.info("Removing trends from time series data\n")
            # Compute the transit duration
            transit_dur = t14(R_s=R_star, M_s=M_star,
                              P=Porb, small_planet=False)

            # Estimate the window length for the detrending
            wl = 3.0 * transit_dur

            # Detrend the time series data
            self.normalized_flux, self.lc_trend = flatten(self.times_clipped,
                                                          self.normalized_flux,
                                                          return_trend=True,
                                                          method="biweight",
                                                          window_length=wl)

        # Standard deviation in ppm for the observation
        self.std = np.nanstd(self.normalized_flux)

        # Binned data and its standard deviation in ppm
        self.binned_data = bin_dataframe(self.normalized_flux, self.binsize)
        self.std_binned = np.nanstd(self.binned_data)

        # Binned times
        self.binned_dates = bin_dataframe(self.times_clipped,
                                          self.binsize,
                                          bin_dates=True)

        # Output directory
        self.output_dir_name = "TimeSeries_Analysis"

        if save_rms:
            # Output directory for files that contain photometric precisions
            output_directory = self.data_directory + self.output_dir_name + "/rms_precisions"
            os.makedirs(output_directory, exist_ok=True)

            # File with rms information
            file_rms_name = os.path.join(output_directory,
                                         f"rms_{self.instrument}.txt")

            with open(file_rms_name, "a") as file:
                file.write(f"{self.r} {self.std} {self.std_binned} "
                           f"{np.nanmedian(S_to_N_obj)} {np.nanmedian(S_to_N_ref)} "
                           f"{np.nanmedian(S_to_N_diff)}\n")

        Outputs = namedtuple("Outputs",
                             "target_flux_sec total_ref_flux_sec sigma_error times")

        return Outputs(self.target_flux_sec, total_reference_flux_sec,
                       self.sigma_total, self.times)

    # @logged
    def plot_lightcurve(self):
        """Plot a light curve using the flux time series data.
        """
        pd.plotting.register_matplotlib_converters()

        # Total time for binsize
        nbin_tot = self.exptime * self.binsize

        # Output directory for lightcurves
        lightcurves_directory = self.data_directory + self.output_dir_name

        # lightcurve name
        lightcurve_name = os.path.join(lightcurves_directory, "Lightcurve_camera_"
                                       f"{self.instrument}_r{self.r}.png")

        fig, ax = plt.subplots(4, 1,
                               sharey="row", sharex="col", figsize=(10, 10))
        fig.suptitle(f"Differential Photometry\nTarget Star {self.star_id}, "
                     f"Aperture Radius = {self.r} pix", fontsize=13)

        ax[3].plot(self.times_clipped, self.normalized_flux, "k.", ms=3,
                   label=f"NBin = {self.exptime:.3f} d, std = {self.std:.2%}")
        ax[3].plot(self.binned_dates, self.binned_data, "ro", ms=4,
                   label=f"NBin = {nbin_tot:.3f} d, std = {self.std_binned:.2%}")
        # ax[3].errorbar(self.times, self.normalized_flux, yerr=self.sigma_total,
        #                fmt="none", ecolor="k", elinewidth=0.8,
        #                label="$\sigma_{\mathrm{tot}}=\sqrt{\sigma_{\mathrm{phot}}^{2} "
        #                "+ \sigma_{\mathrm{sky}}^{2} + \sigma_{\mathrm{scint}}^{2} + "
        #                "\sigma_{\mathrm{read}}^{2}}$",
        #                capsize=0.0)

        ax[3].set_ylabel("Relative\nFlux", fontsize=13)
        # ax[3].legend(fontsize=9.0, loc="lower left", ncol=3, framealpha=1.0)
        # ax[3].set_ylim((0.9995, 1.0004))
        ax[3].ticklabel_format(style="plain", axis="both", useOffset=False)
        loc_x3 = plticker.MultipleLocator(base=5)  # this locator puts ticks at regular intervals
        ax[3].xaxis.set_major_locator(loc_x3)
        ax[3].xaxis.set_major_formatter(plticker.FormatStrFormatter('%.1f'))

        # Plot of target star flux
        ax[2].plot(self.times,
                   self.target_flux_sec / np.nanmean(self.target_flux_sec),
                   "ro", label=f"Target star {self.star_id}", lw=0.0, ms=1.3)
        ax[2].set_ylabel("Normalized\nFlux", fontsize=13)
        ax[2].legend(fontsize=8.6, loc="lower left", ncol=1,
                     framealpha=1.0, frameon=True)
        ax[2].set_ylim((0.9, 1.05))

        ax[0].plot(self.times, self.x_pos_target, "ro-",
                   label="dx [Dec axis]", lw=0.5, ms=1.2)
        ax[0].plot(self.times, self.y_pos_target, "go-",
                   label="dy [RA axis]", lw=0.5, ms=1.2)
        ax[0].set_ylabel(r"$\Delta$ Pixel", fontsize=13)
        ax[0].legend(fontsize=8.6, loc="lower left", ncol=2, framealpha=1.0)
        ax[0].set_title(f"Camera: {self.instrument}", fontsize=13)

        for counter in range(len(self.list_reference_stars)):
            # ax[1].xaxis.set_major_formatter(dates.DateFormatter("%H:%M:%S"))

            # Colors for comparison stars
            # colors = ["blue", "magenta", "green", "cyan", "firebrick"]

            ax[1].plot(self.times, self.reference_star_flux_sec[counter]
                       / np.nanmean(self.reference_star_flux_sec[counter]),
                       "o", ms=1.3, label=f"Star {self.list_reference_stars[counter]}")
            ax[1].set_ylabel("Normalized\nFlux", fontsize=13)
            ax[1].set_ylim((0.9, 1.05))
            ax[1].legend(fontsize=8.1, loc="lower left",
                         ncol=len(self.list_reference_stars),
                         framealpha=1.0, frameon=True)

        ax[3].text(0.97, 0.07, "d)", fontsize=11, transform=ax[3].transAxes)
        ax[2].text(0.97, 0.07, "c)", fontsize=11, transform=ax[2].transAxes)
        ax[1].text(0.97, 0.07, "b)", fontsize=11, transform=ax[1].transAxes)
        ax[0].text(0.97, 0.07, "a)", fontsize=11, transform=ax[0].transAxes)

        # Wasp 29 times
        # ingress = datetime(2019, 10, 27, 10, 34, 00)
        # mid = datetime(2019, 10, 27, 11, 54, 00)
        # egress = datetime(2019, 10, 27, 13, 13, 00)

        # # Transit ingress, mid and egress times
        # plt.axvline(x=ingress, color="k", ls="--")
        # plt.axvline(x=mid, color="b", ls="--")
        # plt.axvline(x=egress, color="k", ls="--")
        # plt.gca().xaxis.set_major_formatter(dates.DateFormatter("%H:%M:%S"))
        plt.xlabel("Time [BJD-2457000.0]", fontsize=13)
        plt.xticks(rotation=30, size=8.0)
        plt.savefig(lightcurve_name)

        logger.info(f"The light curve of {self.star_id} was plotted")

        fig, ax = plt.subplots(1, 1,
                               sharey="row", sharex="col", figsize=(13, 10))
        fig.suptitle(f"Evolution of Noise Sources for the Target Star {self.star_id} "
                     "($m_\mathrm{V}=10.9$)\n"
                     f"Huntsman Defocused Camera {self.instrument}, G Band Filter\n"
                     f"Sector 2", fontsize=15)
        ax.plot_date(self.times, self.sigma_total * 100, "k-",
                     label="$\sigma_{\mathrm{total}}$")
        ax.plot_date(self.times, self.sigma_phot * 100, color="firebrick", marker=None,
                     ls="-", label="$\sigma_{\mathrm{phot}}$")
        ax.plot_date(self.times, self.sigma_sky * 100,
                     "b-", label="$\sigma_{\mathrm{sky}}$")
        ax.plot_date(self.times, self.sigma_ron * 100,
                     "r-", label="$\sigma_{\mathrm{read}}$")
        ax.legend(loc="upper center", bbox_to_anchor=(0.5, 0.998), fancybox=True,
                  ncol=5, frameon=True, fontsize=15)
        # ax.set_yscale("log")
        ax.tick_params(axis="both", direction="in", labelsize=15)
        ax.set_ylabel("Amplitude Error [%]", fontsize=17)
        plt.xticks(rotation=30)
        ax.set_xlabel("Time [UTC]", fontsize=17)
        # ax.set_ylim((0.11, 0.48))
        ax.xaxis.set_major_formatter(dates.DateFormatter("%H:%M:%S"))
        plt.grid(alpha=0.4)
        fig.savefig("noises.png")
