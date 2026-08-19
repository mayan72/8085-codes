def download_ai_forecast_logs(request):
    """
    Download AI forecast tracker Excel and base_forecast log as a zip.
    """
    try:
        tracker_dir = os.path.join(str(settings.MEDIA_ROOT), "ai_forecast_tracking")
        log_path = os.path.join(str(settings.BASE_DIR), "logs", "base_forecast_log.log")

        files_to_zip = []

        if os.path.isdir(tracker_dir):
            for name in os.listdir(tracker_dir):
                file_path = os.path.join(tracker_dir, name)
                if os.path.isfile(file_path):
                    files_to_zip.append(
                        (file_path, os.path.join("ai_forecast_tracking", name))
                    )

        if os.path.isfile(log_path):
            files_to_zip.append(
                (log_path, os.path.join("logs", "base_forecast_log.log"))
            )

        if not files_to_zip:
            raise FileNotFoundError(
                "No AI forecast tracker Excel or base_forecast_log.log found"
            )

        current_datetime = datetime.datetime.now().strftime("%Y-%m-%d")
        zip_filename = f"ai_forecast_logs_{current_datetime}.zip"
        zip_filepath = os.path.join("/tmp", zip_filename)

        with zipfile.ZipFile(zip_filepath, "w") as zipf:
            for source_path, arcname in files_to_zip:
                zipf.write(source_path, arcname)

        with open(zip_filepath, "rb") as f:
            result_file = f.read()

        response = HttpResponse(result_file, content_type="application/zip")
        response.status_code = 200
        response["Content-Disposition"] = f'attachment; filename="{zip_filename}"'

        logger.info("Download AI Forecast Logs: File Served Successfully")

        os.remove(zip_filepath)
    except:
        logger.exception("Download AI Forecast Logs: Error Occurred -")
        messages.add_message(
            request,
            messages.ERROR,
            "Error occurred while downloading AI forecast logs.",
        )
        return HttpResponseRedirect(reverse_lazy("cost_structure:bulk_upload_cs"))

    return response
